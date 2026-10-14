"""Duplicate a repository: ``POST /api/{type}s/{ns}/{name}/duplicate``.

The copy is made in LakeFS, not file by file. A commit is a metarange (its
tree) plus a record, and an object is a key in the repository's storage
namespace; so copying a repository is copying keys on the storage side, in
parallel, and storing commit records for the metaranges now there (as
``squash`` does):

- a Space arrives with the source's whole history: every commit reachable from
  main, stored under its own id, so git history, ids and dates are the source's;
- a model or dataset arrives as one commit of main's head (the Hub squashes them).

LFS objects are global and stay where they are: the copy links them. Branches
other than main and tags are not copied.

The database follows: file rows, LFS history and head links, the commit rows
(a Space's), then the usage counters are recounted. The destination is held
(``operation_lock``) while it is built, and removed if that fails; one a crash
left behind is removed by ``kohakuhub.duplicate_sweep``.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from kohakuhub import path_commits, usage
from kohakuhub.api.commit.routers.operations import RepoType
from kohakuhub.api.commit.squash import commit_address
from kohakuhub.api.quota.util import check_quota
from kohakuhub.api.repo.routers.crud import (
    CreateRepoPayload,
    DeleteRepoPayload,
    _resolve_create_repo_private,
    create_repo,
    delete_repo,
)
from kohakuhub.api.repo.utils import operation_lock
from kohakuhub.api.repo.utils.hf import hf_bad_request, hf_repo_not_found, hf_server_error
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.auth.dependencies import get_current_user
from kohakuhub.auth.permissions import check_repo_read_permission
from kohakuhub.db import Commit, File, LFSObjectHistory, LfsHeadRef, Repository, User, db
from kohakuhub.db_operations import create_commit, get_organization, get_repository
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo
from kohakuhub.utils.s3 import get_s3_client, parse_s3_uri

logger = get_logger("DUPLICATE")
router = APIRouter()

PAGE = 1000
COPY_WORKERS = 64  # parallel storage-side key copies
ROWS = 500  # rows per insert
RENEW_SECONDS = 20  # the destination's lock lives LOCK_SECONDS; renewed this often
READ_TRIES = 6
SNAPSHOT_WAIT_SECONDS = 60  # for the source, while another duplicate takes its snapshot
OPERATION = "duplicate"  # the new repository's lock; the sweep looks for it
SOURCE_OPERATION = "duplicate-source"  # the source's, only while its snapshot is read: never swept


class DuplicatePayload(BaseModel):
    """Space settings (hardware, secrets, ...) are accepted and ignored: no runtime here."""

    repository: str
    private: Optional[bool] = None
    visibility: Optional[str] = None


# ---- LakeFS reading -------------------------------------------------------------------------


async def _pages(fetch):
    after = ""
    while True:
        page = await fetch(after)
        for entry in page.get("results") or []:
            yield entry
        pagination = page.get("pagination") or {}
        if not pagination.get("has_more"):
            return
        after = pagination.get("next_offset") or ""


async def _entries(client, lakefs_repo: str, commit: str) -> list[dict]:
    fetch = lambda after: client.list_objects(  # noqa: E731
        repository=lakefs_repo, ref=commit, after=after, amount=PAGE
    )
    return [e async for e in _pages(fetch)]


async def _ancestors(client, lakefs_repo: str, head: str) -> list[dict]:
    """Every commit ``head`` reaches, parents before children."""
    log = lambda after: client.log_commits(  # noqa: E731
        repository=lakefs_repo, ref=head, after=after or None, amount=PAGE
    )
    commits = [c async for c in _pages(log)]
    return sorted(commits, key=lambda c: (c["generation"], c["creation_date"]))


async def _prefix(client, lakefs_repo: str) -> tuple[str, str]:
    """The bucket and key prefix of a repository's storage namespace."""
    namespace = (await client.get_repository(repository=lakefs_repo))["storage_namespace"]
    bucket, key = parse_s3_uri(namespace)
    return bucket, key.strip("/") + "/"


# ---- storage-side copy ----------------------------------------------------------------------


def _list_keys_sync(bucket: str, prefix: str) -> list[str]:
    paginator = get_s3_client().get_paginator("list_objects_v2")
    return [o["Key"] for page in paginator.paginate(Bucket=bucket, Prefix=prefix) for o in page.get("Contents", [])]


def _copy_keys_sync(bucket: str, pairs: list[tuple[str, str]]) -> int:
    """Copy keys inside the bucket, in parallel; the first failure raises."""
    s3 = get_s3_client(COPY_WORKERS)

    def one(pair):
        s3.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": pair[0]}, Key=pair[1])

    with ThreadPoolExecutor(COPY_WORKERS) as pool:
        list(pool.map(one, pairs))
    return len(pairs)


async def _copy_storage(client, src_lakefs: str, dest_lakefs: str, head_entries: list[dict] | None) -> int:
    """Copy the source's keys to the destination's namespace: every one for a whole history,
    else the metaranges (``_lakefs/``) and the regular objects ``head_entries`` link."""
    bucket, src = await _prefix(client, src_lakefs)
    _, dest = await _prefix(client, dest_lakefs)
    keys = await run_in_s3_executor(_list_keys_sync, bucket, src)
    if head_entries is not None:
        linked = {e["physical_address"] for e in head_entries}  # LFS objects are outside the namespace
        keys = [k for k in keys if k[len(src):].startswith("_lakefs/") or f"s3://{bucket}/{k}" in linked]
    pairs = [(k, dest + k[len(src):]) for k in keys if k[len(src):] != "_lakefs/dummy"]
    return await run_in_s3_executor(_copy_keys_sync, bucket, pairs)


# ---- locks ----------------------------------------------------------------------------------


@asynccontextmanager
async def _building(dest: Repository):
    """Hold the destination while it is built, renewing the lock. A crash never reaches the
    release: the lock lapses and the sweep finds the repository by its token."""
    token = operation_lock.acquire(dest.id, OPERATION)
    if token is None:
        raise HTTPException(409, detail={"error": "The new repository is busy"})

    async def renew():
        while operation_lock.renew(dest.id, token):
            await asyncio.sleep(RENEW_SECONDS)

    renewing = asyncio.create_task(renew())
    try:
        yield
    finally:
        renewing.cancel()
        operation_lock.release(dest.id, token)


async def _snapshot(src: Repository, read):
    """``read()`` while no write moves the source, so the rows it reads belong to the head it
    reads. Waits while another duplicate holds the source."""
    deadline = time.monotonic() + SNAPSHOT_WAIT_SECONDS
    while (token := operation_lock.acquire(src.id, SOURCE_OPERATION)) is None:
        if time.monotonic() > deadline:
            raise HTTPException(409, detail={"error": "The source is busy; try again"})
        await asyncio.sleep(0.2)
    try:
        await operation_lock.drain(src, SOURCE_OPERATION)
        return await read()
    finally:
        operation_lock.release(src.id, token)


# ---- database -------------------------------------------------------------------------------


def _read_rows(src: Repository) -> dict:
    main = usage.MAIN
    return {
        "files": list(File.select().where((File.repository == src) & (File.branch == main)).dicts()),
        "heads": list(LfsHeadRef.select().where((LfsHeadRef.repository == src) & (LfsHeadRef.branch == main)).dicts()),
        "history": list(LFSObjectHistory.select().where(LFSObjectHistory.repository == src).dicts()),
        "commits": list(Commit.select().where(Commit.repository == src).dicts()),
        "history_root": Repository.select(Repository.history_root).where(Repository.id == src.id).scalar(),
    }


def _batched(model, rows: list[dict]) -> None:
    for start in range(0, len(rows), ROWS):
        model.insert_many(rows[start : start + ROWS]).execute()


def _moved(row: dict, **over) -> dict:
    """``row`` as a new row: without its id, with ``over``."""
    return {**{k: v for k, v in row.items() if k != "id"}, **over}


def _write_rows(dest: Repository, rows: dict, commit_ids: set[str], single: str | None, user: User) -> None:
    """The destination's rows, in one transaction: ``single`` is a model's one commit id, else
    the rows of the commits the history holds (``commit_ids``)."""
    old_paths = {f["id"]: f["path_in_repo"] for f in rows["files"]}
    with db.atomic():
        _batched(File, [_moved(f, repository=dest.id, owner=dest.owner_id) for f in rows["files"]])
        new_ids = {
            row.path_in_repo: row.id
            for row in File.select(File.id, File.path_in_repo).where(
                (File.repository == dest) & (File.branch == usage.MAIN)
            )
        }
        _batched(LfsHeadRef, [_moved(h, repository=dest.id) for h in rows["heads"]])
        if single:  # one commit links what main's head links
            history = [
                {
                    "repository": dest.id, "path_in_repo": f["path_in_repo"], "sha256": f["sha256"],
                    "size": f["size"], "commit_id": single, "file": new_ids.get(f["path_in_repo"]),
                }
                for f in rows["files"] if f["lfs"] and not f["is_deleted"]
            ]
            create_commit(
                commit_id=single, repository=dest, repo_type=dest.repo_type, branch=usage.MAIN,
                author=user, username=user.username, message=f"Duplicate of {rows['source']}", description="",
            )
        else:
            history = [
                _moved(h, repository=dest.id, file=new_ids.get(old_paths.get(h["file"])))
                for h in rows["history"] if h["commit_id"] in commit_ids
            ]
            _batched(Commit, [
                _moved(c, repository=dest.id, owner=dest.owner_id)
                for c in rows["commits"] if c["commit_id"] in commit_ids
            ])
            if rows["history_root"]:
                Repository.update(history_root=rows["history_root"]).where(Repository.id == dest.id).execute()
        _batched(LFSObjectHistory, history)


# ---- the copy -------------------------------------------------------------------------------


async def _retrying(call):
    """``await call()``, again after a 5xx: LakeFS fails the first read of a metaranges that
    several repositories open at once (its local cache, "open file after download")."""
    for attempt in range(READ_TRIES - 1):
        try:
            return await call()
        except httpx.HTTPStatusError as error:
            if error.response.status_code < 500:
                raise
            await asyncio.sleep(0.5 * 2**attempt)
    return await call()  # the last try says what it says


async def _store_commits(client, dest_lakefs: str, commits: list[dict]) -> None:
    for c in commits:
        await client.create_commit_record(
            repository=dest_lakefs, commit_id=c["id"], committer=c["committer"], message=c["message"],
            metarange_id=c["meta_range_id"], creation_date=c["creation_date"], parents=c["parents"],
            metadata=c.get("metadata") or {}, generation=c["generation"], version=c.get("version", 1),
        )


def _squash_commit(head_commit: dict, source: str, user: User) -> dict:
    """The one commit a model or dataset gets: head's tree, no parent."""
    commit = {
        "committer": user.username, "message": f"Duplicate of {source}", "meta_range_id": head_commit["meta_range_id"],
        "creation_date": int(time.time()), "parents": [], "generation": 1, "version": 1,
        "metadata": {"kh_operation": OPERATION, "kh_duplicated": head_commit["id"]},
    }
    commit["id"] = commit_address(
        commit["committer"], commit["message"], commit["meta_range_id"], commit["creation_date"],
        commit["metadata"], commit["parents"],
    )
    return commit


async def _copy(src: Repository, dest: Repository, user: User, whole_history: bool) -> int:
    """Build ``dest`` from ``src``'s main; the commits it holds."""
    client = get_lakefs_client()
    src_lakefs, dest_lakefs = resolve_lakefs_repo(src), resolve_lakefs_repo(dest)

    async def read():
        head = (await client.get_branch(repository=src_lakefs, branch=usage.MAIN))["commit_id"]
        rows = _read_rows(src)
        rows["source"] = src.full_id
        return head, rows, None if whole_history else await _entries(client, src_lakefs, head)

    started = time.monotonic()
    lap = lambda what: logger.info(f"{src.full_id} -> {dest.full_id}: {what} at {time.monotonic() - started:.2f}s")  # noqa: E731
    head, rows, head_entries = await _snapshot(src, read)
    lap(f"snapshot ({len(rows['files'])} file rows)")
    # Immutable from here on: the commits, their metaranges, the objects
    commits = await _ancestors(client, src_lakefs, head)
    stored = commits if whole_history else [_squash_commit(next(c for c in commits if c["id"] == head), src.full_id, user)]
    copied = await _copy_storage(client, src_lakefs, dest_lakefs, head_entries)
    lap(f"{copied} keys copied")
    # Rows first: what the destination's head links is known to garbage collection before it exists
    _write_rows(dest, rows, {c["id"] for c in commits}, None if whole_history else stored[0]["id"], user)
    lap("rows written")
    await _store_commits(client, dest_lakefs, stored)
    try:
        await client.hard_reset_branch(repository=dest_lakefs, branch=usage.MAIN, ref=stored[-1]["id"], force=True)
    except httpx.HTTPStatusError as error:
        raise RuntimeError(f"LakeFS would not move main to the copy: {error}") from error
    lap(f"{len(stored)} commit(s) stored, main moved")
    await _retrying(lambda: path_commits.backfill_repository(client, dest))
    lap("last commits recorded")
    await _retrying(lambda: usage.recount_repository(dest.id))
    lap("usage recounted")
    return len(stored)


async def _bytes_to_copy(src: Repository, whole_history: bool) -> int:
    """What the copy adds to its namespace's usage: a history's LFS versions all count
    (as the source's own usage does), a single commit holds the head only."""
    if whole_history:
        return src.used_bytes
    client, lakefs_repo = get_lakefs_client(), resolve_lakefs_repo(src)
    head = (await client.get_branch(repository=lakefs_repo, branch=usage.MAIN))["commit_id"]
    return sum(e.get("size_bytes") or 0 for e in await _entries(client, lakefs_repo, head))


@router.post("/{repo_type}s/{namespace}/{name}/duplicate")
async def duplicate_repo(
    repo_type: RepoType,
    namespace: str,
    name: str,
    payload: DuplicatePayload,
    user: User = Depends(get_current_user),
):
    src = get_repository(repo_type.value, namespace, name)
    if src is None:
        return hf_repo_not_found(f"{namespace}/{name}", repo_type.value)
    check_repo_read_permission(src, user)

    dest_ns, _, dest_name = payload.repository.partition("/")
    if not dest_ns or not dest_name or "/" in dest_name:
        return hf_bad_request("repository must be 'namespace/name'")
    create = CreateRepoPayload(
        type=repo_type.value, name=dest_name, organization=dest_ns,
        private=payload.private, visibility=payload.visibility,
    )
    if payload.private is None and payload.visibility is None:
        create.private = src.private  # the Hub: the copy is as private as the original
    else:
        _resolve_create_repo_private(create)  # 400 for a visibility we do not have

    whole_history = repo_type.value == "space"  # a Space keeps its git history: ids, dates, all
    allowed, why = check_quota(
        dest_ns, await _bytes_to_copy(src, whole_history), create.private,
        get_organization(dest_ns) is not None,
    )
    if not allowed:  # as preupload answers
        raise HTTPException(413, detail={"error": "Storage quota exceeded", "message": why})

    created = await create_repo(create, user)
    if isinstance(created, Response):  # 409 with the url, a refusal, a failure: as create_repo says
        return created

    dest = get_repository(repo_type.value, dest_ns, dest_name)
    try:
        async with _building(dest):
            commits = await _copy(src, dest, user, whole_history)
    except Exception as error:
        logger.exception(f"Duplicating {src.full_id} to {dest.full_id} failed; removing the copy", error)
        # No half-made copy is left behind
        removed = await delete_repo(
            DeleteRepoPayload(type=repo_type.value, name=dest_name, organization=dest_ns), (user, False)
        )
        if isinstance(removed, Response):
            logger.error(f"Could not remove the failed copy {dest.full_id}: {removed.status_code}")
        # Always a 500: a 409 from a step inside would read as "the copy exists" to a client's exist_ok
        detail = error.detail.get("error") if isinstance(error, HTTPException) and isinstance(error.detail, dict) else str(error)
        return hf_server_error(f"Duplicating {src.full_id} failed: {detail}")
    logger.info(f"Duplicated {src.full_id} to {dest.full_id} ({commits} commit(s))")
    return {"url": created["url"], "filesCopyPending": False}
