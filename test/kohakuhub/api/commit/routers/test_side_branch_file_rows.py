"""Reproduction for #11: a commit to a side branch rewrites main's File row."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager

import pytest

import kohakuhub.api.commit.routers.operations as commit_ops
from kohakuhub.api.repo.routers.tree import _build_file_record_map
from kohakuhub.db import File
from test.kohakuhub.support.factories import make_file, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_fresh")


class _Request:
    def __init__(self, body: bytes):
        self._body = body
        self.query_params = {}

    async def body(self):
        return self._body


class _LakeFS:
    def __init__(self):
        self.branch_data = {"commit_id": "head-commit"}
        self.commit_data = {"id": "commit-created"}

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
        return self.commit_data

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
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: _LakeFS())
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


# --- stage 2: every File write carries its branch ----------------------------------

from kohakuhub.api.commit.routers.operations import calculate_git_blob_sha1  # noqa: E402
from kohakuhub.api.files import check_file_by_sha256, process_preupload_file  # noqa: E402
from kohakuhub.db_operations import get_file, get_repo_file_metadata_map  # noqa: E402


def _blob(content: bytes) -> str:
    return calculate_git_blob_sha1(content)


def _row(repo, branch: str, path: str):
    return File.get_or_none(
        (File.repository == repo) & (File.branch == branch) & (File.path_in_repo == path)
    )


def _body(*records) -> bytes:
    lines = [{"key": "header", "value": {"summary": "change"}}]
    lines.extend({"key": key, "value": value} for key, value in records)
    return "\n".join(json.dumps(line) for line in lines).encode("utf-8")


def _file(path: str, content: bytes) -> tuple:
    return "file", {"path": path, "content": base64.b64encode(content).decode(), "encoding": "base64"}


def _setup(monkeypatch, client=None):
    client = client or _LakeFS()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(commit_ops.operation_lock, "writing", _no_lock)
    repo = make_repo(make_user("owner"), "repo")
    return client, repo


async def _commit(repo, revision: str, body: bytes):
    return await commit_ops.commit(
        commit_ops.RepoType.model, repo.namespace, repo.name, revision,
        _Request(body), user=repo.owner,
    )


@pytest.mark.asyncio
async def test_side_branch_regular_commit_writes_its_own_row(monkeypatch):
    _, repo = _setup(monkeypatch)
    await _commit(repo, "main", _body(_file("a.txt", b"hello")))
    await _commit(repo, "dev", _body(_file("a.txt", b"world")))
    assert _row(repo, "dev", "a.txt").sha256 == _blob(b"world")
    assert _row(repo, "main", "a.txt").sha256 == _blob(b"hello")


@pytest.mark.asyncio
async def test_side_branch_lfs_object_writes_its_own_row(monkeypatch):
    client, repo = _setup(monkeypatch)
    monkeypatch.setattr(commit_ops, "object_exists", _always_true)
    oid = "a" * 64
    await commit_ops.process_lfs_file("big.bin", oid, 7, "sha256", repo, "repo", "dev")
    assert _row(repo, "dev", "big.bin").lfs is True
    assert _row(repo, "dev", "big.bin").sha256 == oid
    assert _row(repo, "main", "big.bin") is None


async def _always_true(bucket, key):
    return True


@pytest.mark.asyncio
async def test_side_branch_delete_marks_only_its_own_row(monkeypatch):
    _, repo = _setup(monkeypatch)
    await _commit(repo, "main", _body(_file("a.txt", b"hello")))
    await _commit(repo, "dev", _body(_file("a.txt", b"world")))
    await _commit(repo, "dev", _body(("deletedFile", {"path": "a.txt"})))
    assert _row(repo, "dev", "a.txt").is_deleted is True
    assert _row(repo, "main", "a.txt").is_deleted is False


class _FolderLakeFS(_LakeFS):
    def __init__(self, listing):
        super().__init__()
        self.listing = listing

    async def list_objects(self, **kwargs):
        return {
            "results": [
                {"path": path, "path_type": "object"} for path in self.listing
            ]
        }


@pytest.mark.asyncio
async def test_side_branch_folder_delete_marks_only_its_own_rows(monkeypatch):
    client, repo = _setup(monkeypatch, _FolderLakeFS(["folder/a.txt"]))
    await _commit(repo, "main", _body(_file("folder/a.txt", b"hello")))
    await _commit(repo, "dev", _body(_file("folder/a.txt", b"world")))
    await _commit(repo, "dev", _body(("deletedFolder", {"path": "folder"})))
    assert _row(repo, "dev", "folder/a.txt").is_deleted is True
    assert _row(repo, "main", "folder/a.txt").is_deleted is False


@pytest.mark.asyncio
async def test_side_branch_copy_writes_its_own_row(monkeypatch):
    client, repo = _setup(monkeypatch)
    await _commit(repo, "main", _body(_file("src.txt", b"hello")))
    await _commit(
        repo, "dev", _body(("copyFile", {"path": "copy.txt", "srcPath": "src.txt", "srcRevision": "main"}))
    )
    assert _row(repo, "dev", "copy.txt") is not None
    assert _row(repo, "main", "copy.txt") is None


class _FailingUploadLakeFS(_LakeFS):
    """The second upload fails, after the first file's row is already written."""

    def __init__(self, fail_path):
        super().__init__()
        self.fail_path = fail_path

    async def upload_object(self, **kwargs):
        if kwargs["path"] == self.fail_path:
            raise RuntimeError("lakefs is down")
        return {"ok": True}


@pytest.mark.asyncio
async def test_failed_side_branch_commit_restores_its_own_rows(monkeypatch):
    client, repo = _setup(monkeypatch)
    await _commit(repo, "main", _body(_file("a.txt", b"hello")))
    await _commit(repo, "dev", _body(_file("a.txt", b"old")))
    client.upload_object = _FailingUploadLakeFS("b.txt").upload_object
    with pytest.raises(Exception):
        await _commit(repo, "dev", _body(_file("a.txt", b"new"), _file("b.txt", b"x")))
    assert _row(repo, "dev", "a.txt").sha256 == _blob(b"old")
    assert _row(repo, "main", "a.txt").sha256 == _blob(b"hello")


def test_metadata_map_and_sha_check_read_the_requested_branch(db_fresh):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "a.txt", "1" * 40, size=5, branch="main")
    make_file(repo, "a.txt", "2" * 40, size=5, branch="dev")
    assert get_repo_file_metadata_map(repo, ["a.txt"], branch="main")["a.txt"] == ("1" * 40, 5)
    assert get_repo_file_metadata_map(repo, ["a.txt"], branch="dev")["a.txt"] == ("2" * 40, 5)
    assert get_file(repo, "a.txt", branch="dev").sha256 == "2" * 40
    assert get_file(repo, "a.txt").sha256 == "1" * 40


@pytest.mark.asyncio
async def test_preupload_dedupe_uses_the_branch_rows(db_fresh):
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "a.txt", "3" * 40, size=5, branch="main")
    assert await check_file_by_sha256(repo, "a.txt", "3" * 40, 5, branch="main") is True
    assert await check_file_by_sha256(repo, "a.txt", "3" * 40, 5, branch="dev") is False


# --- stage 3: reads take their branch's rows ---------------------------------------


def test_tree_map_reads_the_named_branch_only():
    from test.kohakuhub.support.factories import make_file

    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", "1" * 40, branch="main")
    make_file(repo, "README.md", "2" * 40, branch="dev")

    assert _build_file_record_map(repo, ["README.md"], "dev")["README.md"].sha256 == "2" * 40
    assert _build_file_record_map(repo, ["README.md"])["README.md"].sha256 == "1" * 40


@pytest.mark.asyncio
async def test_copy_from_a_commit_records_the_git_blob_id(monkeypatch):
    """A regular file copied from a commit (no row of its own) gets the git blob id
    as its checksum, as the tree and the blobs manifest expect (#11, stage 3)."""

    class _Content(_LakeFS):
        async def get_object(self, **kwargs):
            return b"hello"

    client, repo = _setup(monkeypatch, _Content())

    async def no_history(*args, **kwargs):
        return None

    monkeypatch.setattr(commit_ops, "ensure_revision_in_history", no_history)
    await commit_ops.process_copy_file(
        "copied.md", "README.md", "commit-id", repo, "lakefs-repo", "main"
    )
    assert _row(repo, "main", "copied.md").sha256 == _blob(b"hello")


def test_blobs_manifest_reads_the_main_rows_only():
    from kohakuhub.api.repo.utils.hf import _regular_blob_ids
    from test.kohakuhub.support.factories import make_file

    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", "1" * 40, branch="main")
    make_file(repo, "README.md", "2" * 40, branch="dev")

    assert _regular_blob_ids(repo, "main") == {"README.md": "1" * 40}
    assert _regular_blob_ids(repo, "dev") == {"README.md": "2" * 40}
