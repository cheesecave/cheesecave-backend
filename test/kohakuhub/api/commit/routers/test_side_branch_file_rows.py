"""A commit to a side branch must not rewrite main's File rows (#11, plan B)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import kohakuhub.api.commit.routers.operations as commit_ops
from kohakuhub.api.repo.routers.tree import _build_file_record_map
from kohakuhub.db import File, LFSObjectHistory
from test.kohakuhub.support.factories import make_file, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


class _Request:
    def __init__(self, body: bytes):
        self._body = body
        self.query_params = {}

    async def body(self):
        return self._body


class _LakeFS:
    def __init__(self):
        self.branch_data = {"commit_id": "head-commit"}
        self.commits = 0

    async def upload_object(self, **kwargs):
        return {"ok": True}

    async def link_physical_address(self, **kwargs):
        return {"ok": True}

    async def delete_object(self, **kwargs):
        return {"ok": True}

    async def list_objects(self, **kwargs):
        return {"results": []}

    async def stat_object(self, **kwargs):
        return {"physical_address": "s3://b/p", "checksum": "sha256:x", "size_bytes": 5}

    async def get_object(self, **kwargs):
        return b"x"

    async def get_branch(self, **kwargs):
        return self.branch_data

    async def commit(self, **kwargs):
        # Each commit gets its own id, as LakeFS gives them (commits are rows)
        self.commits += 1
        return {"id": f"commit-{self.commits}"}

    async def get_commit(self, **kwargs):
        return {"id": kwargs["commit_id"]}


@asynccontextmanager
async def _no_lock(repo):
    yield


def _file_body(content_b64: str) -> bytes:
    records = [
        {"key": "header", "value": {"summary": "change"}},
        {"key": "file", "value": {"path": "README.md", "content": content_b64, "encoding": "base64"}},
    ]
    return "\n".join(json.dumps(r) for r in records).encode("utf-8")


@pytest.mark.asyncio
async def test_side_branch_commit_leaves_main_file_row_unchanged(monkeypatch):
    client = _LakeFS()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(commit_ops.operation_lock, "writing", _no_lock)
    repo = make_repo(make_user("owner"), "repo")
    user = repo.owner

    async def commit_to(revision: str, content_b64: str):
        return await commit_ops.commit(
            commit_ops.RepoType.model, repo.namespace, repo.name, revision,
            _Request(_file_body(content_b64)), user=user,
        )

    await commit_to("main", "aGVsbG8=")  # "hello" on main
    main_oid = File.get((File.repository == repo) & (File.path_in_repo == "README.md")).sha256

    await commit_to("dev", "d29ybGQ=")  # "world" on a side branch

    # What the tree router reads for every revision (tree.py _build_file_record_map)
    seen_by_main = _build_file_record_map(repo, ["README.md"])["README.md"].sha256
    assert seen_by_main == main_oid


@pytest.mark.asyncio
async def test_side_branch_commit_of_main_content_still_uploads_to_the_branch(monkeypatch):
    client = _LakeFS()
    uploads = []
    original_upload = client.upload_object

    async def recording_upload(**kwargs):
        uploads.append(kwargs["branch"])
        return await original_upload(**kwargs)

    client.upload_object = recording_upload
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(commit_ops.operation_lock, "writing", _no_lock)
    repo = make_repo(make_user("owner"), "repo")
    user = repo.owner

    async def commit_to(revision: str, content_b64: str):
        return await commit_ops.commit(
            commit_ops.RepoType.model, repo.namespace, repo.name, revision,
            _Request(_file_body(content_b64)), user=user,
        )

    await commit_to("main", "aGVsbG8=")
    await commit_to("dev", "aGVsbG8=")  # same bytes as main

    # The dev branch must receive the file; the main-row dedupe skips it instead.
    assert "dev" in uploads


@pytest.mark.asyncio
async def test_main_commit_is_not_skipped_after_a_side_branch_overwrote_the_row(monkeypatch):
    client = _LakeFS()
    uploads = []
    original_upload = client.upload_object

    async def recording_upload(**kwargs):
        uploads.append(kwargs["branch"])
        return await original_upload(**kwargs)

    client.upload_object = recording_upload
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(commit_ops.operation_lock, "writing", _no_lock)
    repo = make_repo(make_user("owner"), "repo")
    user = repo.owner

    async def commit_to(revision: str, content_b64: str):
        return await commit_ops.commit(
            commit_ops.RepoType.model, repo.namespace, repo.name, revision,
            _Request(_file_body(content_b64)), user=user,
        )

    await commit_to("main", "aGVsbG8=")  # main: hello
    await commit_to("dev", "d29ybGQ=")  # dev: world (row now says world)
    uploads.clear()
    await commit_to("main", "d29ybGQ=")  # main now also changes to world

    # main's own content in LakeFS is still "hello"; the upload must not be skipped.
    assert "main" in uploads


# ----- the commit route writes no File row for a side branch -----

OID_OLD = "a" * 64  # an LFS object main's row links
OID_NEW = "b" * 64  # an LFS object only the side branch links
OID_MAIN = "c" * 64
BLOB_HELLO = "b6fc4c620b67d95f953a5c1c1230aaab5db5a1b0"  # git blob of "hello"


class _Lake(_LakeFS):
    """LakeFS stays a fake; it records what the side-branch tests check."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.listing = []
        self.on_commit = None

    async def link_physical_address(self, **kwargs):
        self.links.append((kwargs["branch"], kwargs["path"]))
        return {"ok": True}

    async def list_objects(self, **kwargs):
        return {"results": self.listing}

    async def commit(self, **kwargs):
        if self.on_commit:
            await self.on_commit()
        return await super().commit(**kwargs)


@pytest.fixture
def lake(monkeypatch):
    client = _Lake()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(commit_ops.operation_lock, "writing", _no_lock)
    return client


@pytest.fixture
def stored(monkeypatch):
    """S3 is an environment service: every LFS object is stored, 10 bytes."""

    async def exists(bucket, key):
        return True

    async def metadata(bucket, key):
        return {"size": 10}

    monkeypatch.setattr(commit_ops.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(commit_ops, "object_exists", exists)
    monkeypatch.setattr(commit_ops, "get_object_metadata", metadata)


def _ndjson(*operations) -> bytes:
    records = [{"key": "header", "value": {"summary": "change"}}, *operations]
    return "\n".join(json.dumps(r) for r in records).encode("utf-8")


def _lfs_op(path: str, oid: str) -> dict:
    return {"key": "lfsFile", "value": {"path": path, "oid": oid, "size": 10, "algo": "sha256"}}


def _delete_op(path: str) -> dict:
    return {"key": "deletedFile", "value": {"path": path}}


async def _commit_to(repo, revision: str, *operations):
    return await commit_ops.commit(
        commit_ops.RepoType.model, repo.namespace, repo.name, revision,
        _Request(_ndjson(*operations)), user=repo.owner,
    )


def _row(repo, path):
    return File.get((File.repository == repo) & (File.path_in_repo == path))


async def test_side_branch_lfs_object_that_main_links_is_still_linked_on_the_branch(
    lake, stored
):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "weights.bin", sha256=OID_OLD, size=10, lfs=True)

    await _commit_to(repo, "dev", _lfs_op("weights.bin", OID_OLD))

    # Same content as main's row: the branch still gets the link (no skip)
    assert ("dev", "weights.bin") in lake.links
    assert _row(repo, "weights.bin").sha256 == OID_OLD


async def test_side_branch_delete_leaves_the_main_row_active(lake, stored):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", sha256=BLOB_HELLO, size=5)

    await _commit_to(repo, "dev", _delete_op("README.md"))

    assert _row(repo, "README.md").is_deleted is False


async def test_side_branch_folder_delete_leaves_the_main_rows_active(lake, stored):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "data/a.bin", sha256=OID_OLD, size=10, lfs=True)
    lake.listing = [{"path": "data/a.bin", "path_type": "object"}]

    await _commit_to(repo, "dev", {"key": "deletedFolder", "value": {"path": "data"}})

    assert _row(repo, "data/a.bin").is_deleted is False


async def test_side_branch_copy_writes_no_row_for_the_destination(lake, stored):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "src.txt", sha256=BLOB_HELLO, size=5)
    copy = {
        "key": "copyFile",
        "value": {"path": "copy.txt", "srcPath": "src.txt", "srcRevision": "main"},
    }

    await _commit_to(repo, "dev", copy)

    assert File.get_or_none((File.repository == repo) & (File.path_in_repo == "copy.txt")) is None


async def test_failed_side_branch_commit_keeps_the_main_row_a_main_commit_wrote_meanwhile(
    lake, stored
):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", sha256=BLOB_HELLO, size=5)

    async def main_lands_meanwhile():
        File.update(sha256=OID_MAIN, size=10).where(
            (File.repository == repo) & (File.path_in_repo == "README.md")
        ).execute()
        raise HTTPException(409, detail={"error": "conflict"})

    lake.on_commit = main_lands_meanwhile

    with pytest.raises(HTTPException) as refused:
        await _commit_to(repo, "dev", _file_op("README.md", "d29ybGQ="))  # "world"
    assert refused.value.status_code == 409
    # The undo restores only what the branch changed: main's own write survives
    assert _row(repo, "README.md").sha256 == OID_MAIN


async def test_side_branch_lfs_history_does_not_link_the_main_row(lake, stored):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "weights.bin", sha256=OID_OLD, size=10, lfs=True)

    await _commit_to(repo, "dev", _lfs_op("weights.bin", OID_NEW))

    history = LFSObjectHistory.get(
        (LFSObjectHistory.repository == repo) & (LFSObjectHistory.sha256 == OID_NEW)
    )
    assert history.file_id is None  # main's row describes main's object, not this one


async def test_main_lfs_history_links_the_main_row(lake, stored):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "weights.bin", sha256=OID_OLD, size=10, lfs=True)

    await _commit_to(repo, "main", _lfs_op("weights.bin", OID_NEW))

    history = LFSObjectHistory.get(
        (LFSObjectHistory.repository == repo) & (LFSObjectHistory.sha256 == OID_NEW)
    )
    assert history.file_id == _row(repo, "weights.bin").id


def _file_op(path: str, content_b64: str) -> dict:
    return {"key": "file", "value": {"path": path, "content": content_b64, "encoding": "base64"}}
