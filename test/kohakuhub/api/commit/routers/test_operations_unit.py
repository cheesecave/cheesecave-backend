"""Unit tests for commit operation helpers and router flow, on real repository and file rows."""

from __future__ import annotations

from contextlib import asynccontextmanager
import base64
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

import kohakuhub.api.commit.routers.operations as commit_ops
from kohakuhub.db import File
from botocore.exceptions import ClientError, EndpointConnectionError

from test.kohakuhub.support.factories import make_file, make_repo, make_user
from test.kohakuhub.support.fakes import lakefs_error
from test.kohakuhub.support.grants import grant_lfs_upload_everywhere

pytestmark = pytest.mark.usefixtures("db_scope")


@asynccontextmanager
async def _no_lock(repo):
    yield


@pytest.fixture(autouse=True)
def _repository_not_held(monkeypatch):
    """No history operation holds the repositories (the lock itself is
    tested against the real database in test_super_squash.py)."""
    monkeypatch.setattr(commit_ops.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(commit_ops.operation_lock, "writing", _no_lock)


@pytest.fixture
def repo():
    return make_repo(make_user("owner"), "repo")


class _FakeRequest:
    def __init__(self, body: bytes, query_params: dict | None = None):
        self._body = body
        # Real ``starlette.Request.query_params`` is a QueryParams object,
        # but everything the commit handler does with it goes through
        # ``.get(...)`` — a plain dict suffices for unit tests.
        self.query_params = query_params or {}

    async def body(self):
        return self._body


class _FakeLakeFSClient:
    """LakeFS is an environment service: it stays mocked, its database side is real rows."""

    def __init__(self):
        self.calls = []
        self.branch_data = {"commit_id": "head-commit"}
        self.commit_data = {"id": "commit-created"}
        self.raise_on = {}
        self.list_payload = {"results": []}

    def _maybe_raise(self, name):
        error = self.raise_on.get(name)
        if error:
            raise error

    async def upload_object(self, **kwargs):
        self.calls.append(("upload_object", kwargs))
        self._maybe_raise("upload_object")
        return {"ok": True}

    async def link_physical_address(self, **kwargs):
        self.calls.append(("link_physical_address", kwargs))
        self._maybe_raise("link_physical_address")
        return {"ok": True}

    async def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self._maybe_raise("delete_object")
        return {"ok": True}

    async def list_objects(self, **kwargs):
        self.calls.append(("list_objects", kwargs))
        self._maybe_raise("list_objects")
        return self.list_payload

    async def stat_object(self, **kwargs):
        self.calls.append(("stat_object", kwargs))
        self._maybe_raise("stat_object")
        return {
            "physical_address": "s3://bucket/shared/path",
            "checksum": "sha256:abc",
            "size_bytes": 12,
        }

    async def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        self._maybe_raise("get_object")
        return b"copied-content"

    async def get_branch(self, **kwargs):
        self.calls.append(("get_branch", kwargs))
        self._maybe_raise("get_branch")
        return self.branch_data

    async def commit(self, **kwargs):
        self.calls.append(("commit", kwargs))
        self._maybe_raise("commit")
        return self.commit_data

    async def get_commit(self, **kwargs):
        self.calls.append(("get_commit", kwargs))
        self._maybe_raise("get_commit")
        return {"id": kwargs["commit_id"]}


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()


def _active(repo, path):
    """The file row of ``path`` in ``repo``, deleted or not."""
    return File.get((File.repository == repo) & (File.path_in_repo == path))


def test_calculate_git_blob_sha1_matches_git_blob_format():
    content = b"hello world"

    digest = commit_ops.calculate_git_blob_sha1(content)

    assert digest == "95d09f2b10159347eece71399a7e2e907ea3df4f"


@pytest.mark.asyncio
async def test_process_regular_file_covers_validation_skip_restore_and_success(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops, "get_effective_lfs_threshold", lambda repo_arg: 10)
    monkeypatch.setattr(commit_ops, "should_use_lfs", lambda repo_arg, path, size: size >= 10)

    with pytest.raises(HTTPException) as invalid_encoding:
        await commit_ops.process_regular_file("README.md", "aGVsbG8=", "utf8", repo, "lakefs", "main")
    assert invalid_encoding.value.status_code == 400

    # Real input, not a stub of b64decode: "!!!" decodes to b"" unless it is validated (#99)
    for not_base64 in ("!!", "!!!", "aGV$sbG8="):
        with pytest.raises(HTTPException) as bad_base64:
            await commit_ops.process_regular_file("README.md", not_base64, "base64", repo, "lakefs", "main")
        assert bad_base64.value.status_code == 400
        assert "base64" in bad_base64.value.detail["error"]

    large_content = base64.b64encode(b"0123456789").decode("ascii")
    with pytest.raises(HTTPException) as lfs_required:
        await commit_ops.process_regular_file("README.md", large_content, "base64", repo, "lakefs", "main")
    assert lfs_required.value.status_code == 400
    assert lfs_required.value.detail["suggested_operation"] == "lfsFile"

    # The active row has the same blob sha and size: unchanged, nothing uploaded
    make_file(repo, "README.md", commit_ops.calculate_git_blob_sha1(b"hello"), size=5)
    monkeypatch.setattr(commit_ops, "should_use_lfs", lambda repo_arg, path, size: False)
    skipped = await commit_ops.process_regular_file(
        "README.md",
        base64.b64encode(b"hello").decode("ascii"),
        "base64",
        repo,
        "lakefs",
        "main",
    )
    assert skipped is False
    assert client.calls == []

    # A deleted row is not an active file: the write uploads and un-deletes it
    make_file(repo, "old.md", "old", size=1, is_deleted=True)
    changed = await commit_ops.process_regular_file(
        "old.md",
        base64.b64encode(b"hello").decode("ascii"),
        "base64",
        repo,
        "lakefs",
        "main",
    )
    assert changed is True
    assert client.calls[-1][0] == "upload_object"
    restored = _active(repo, "old.md")
    assert (restored.sha256, restored.size, restored.is_deleted) == (
        commit_ops.calculate_git_blob_sha1(b"hello"),
        5,
        False,
    )

    for refusal, status, code in [
        (lakefs_error(503, "upload failed"), 500, "ServerError"),
        (lakefs_error(404, "branch not found"), 404, "RevisionNotFound"),  # the branch was deleted meanwhile
        (lakefs_error(400, "empty value is not allowed"), 400, "BadRequest"),
    ]:
        client.raise_on["upload_object"] = refusal
        with pytest.raises(HTTPException) as upload_error:
            await commit_ops.process_regular_file(
                "README.md",
                base64.b64encode(b"world").decode("ascii"),
                "base64",
                repo,
                "lakefs",
                "main",
            )
        assert upload_error.value.status_code == status
        assert upload_error.value.headers["X-Error-Code"] == code
        assert upload_error.value.__cause__ is refusal  # the cause stays in the log

    with pytest.raises(HTTPException) as not_base64:
        await commit_ops.process_regular_file("README.md", "not base64!", "base64", repo, "lakefs", "main")
    assert not_base64.value.status_code == 400 and "decode base64" in not_base64.value.detail["error"]


@pytest.mark.asyncio
async def test_process_lfs_file_covers_same_content_new_content_and_failures(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(commit_ops, "object_exists", lambda bucket, key: _async_return(True))
    claims = []
    monkeypatch.setattr(
        commit_ops, "claim_for_commit", lambda oid, exists: claims.append((oid, exists))
    )
    grant_lfs_upload_everywhere("sameoid")
    grant_lfs_upload_everywhere("newoid")

    with pytest.raises(HTTPException) as missing_oid:
        await commit_ops.process_lfs_file("weights.bin", None, 10, "sha256", repo, "lakefs", "main")
    assert missing_oid.value.status_code == 400

    weights = make_file(repo, "weights.bin", "sameoid", size=10, lfs=True, is_deleted=True)
    restored = await commit_ops.process_lfs_file(
        "weights.bin", "sameoid", 10, "sha256", repo, "lakefs", "main"
    )
    assert restored[0] is True
    assert restored[1]["sha256"] == "sameoid"
    assert File.get_by_id(weights.id).is_deleted is False  # restored in the database
    assert claims == [("sameoid", True)]  # protected from collection while linked

    unchanged = await commit_ops.process_lfs_file(
        "weights.bin", "sameoid", 10, "sha256", repo, "lakefs", "main"
    )
    assert unchanged == (
        False,
        {"path": "weights.bin", "sha256": "sameoid", "size": 10, "old_sha256": None},
    )

    File.update(sha256="oldoid", size=8).where(File.id == weights.id).execute()
    monkeypatch.setattr(commit_ops, "object_exists", lambda bucket, key: _async_return(False))
    with pytest.raises(HTTPException) as missing_object:
        await commit_ops.process_lfs_file("weights.bin", "newoid", 11, "sha256", repo, "lakefs", "main")
    assert missing_object.value.status_code == 400

    monkeypatch.setattr(commit_ops, "object_exists", lambda bucket, key: _async_return(True))
    monkeypatch.setattr(commit_ops, "get_object_metadata", lambda bucket, key: _async_return({"size": 12}))
    changed, tracking = await commit_ops.process_lfs_file(
        "weights.bin", "newoid", 11, "sha256", repo, "lakefs", "main"
    )
    assert changed is True
    assert tracking["old_sha256"] == "oldoid"
    assert File.get_by_id(weights.id).sha256 == "newoid"
    assert claims[-1] == ("newoid", True)

    # Content a collection is deleting (or deleted) must be uploaded again
    def unavailable(oid, exists):
        raise commit_ops.LfsObjectUnavailable(oid)

    monkeypatch.setattr(commit_ops, "claim_for_commit", unavailable)
    with pytest.raises(HTTPException) as collected:
        await commit_ops.process_lfs_file("weights.bin", "newoid", 11, "sha256", repo, "lakefs", "main")
    assert collected.value.status_code == 409

    # Revived content is checked again: a collection may have deleted it
    # after the first check
    answers = iter([True, False])
    monkeypatch.setattr(commit_ops, "object_exists", lambda bucket, key: _async_return(next(answers)))
    monkeypatch.setattr(commit_ops, "claim_for_commit", lambda oid, exists: True)
    with pytest.raises(HTTPException) as deleted_meanwhile:
        await commit_ops.process_lfs_file("weights.bin", "newoid", 11, "sha256", repo, "lakefs", "main")
    assert deleted_meanwhile.value.status_code == 409
    monkeypatch.setattr(commit_ops, "object_exists", lambda bucket, key: _async_return(True))
    monkeypatch.setattr(commit_ops, "claim_for_commit", lambda oid, exists: False)

    monkeypatch.setattr(
        commit_ops,
        "get_object_metadata",
        lambda bucket, key: (_ for _ in ()).throw(ClientError({"Error": {"Code": "500", "Message": "meta fail"}}, "HeadObject")),
    )
    for refusal, status in [(lakefs_error(500, "link failed"), 500), (lakefs_error(404, "branch not found"), 404)]:
        client.raise_on["link_physical_address"] = refusal
        with pytest.raises(HTTPException) as link_error:
            await commit_ops.process_lfs_file("weights.bin", "newoid", 11, "sha256", repo, "lakefs", "main")
        assert link_error.value.status_code == status


@pytest.mark.asyncio
async def test_process_deleted_file_and_folder_cover_success_partial_failures_and_exceptions(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    readme = make_file(repo, "README.md", "readme", size=5)

    deleted = await commit_ops.process_deleted_file("README.md", repo, "lakefs", "main")
    assert deleted is True
    assert File.get_by_id(readme.id).is_deleted is True

    # LakeFS failing is a failure of the commit: the file stays active in the database while LakeFS has it (#10)
    again = make_file(repo, "kept.md", "kept", size=4)
    for refusal, status in [(lakefs_error(500, "delete failed"), 500), (lakefs_error(404, "branch not found"), 404)]:
        client.raise_on["delete_object"] = refusal
        with pytest.raises(HTTPException) as delete_error:
            await commit_ops.process_deleted_file("kept.md", repo, "lakefs", "main")
        assert delete_error.value.status_code == status
        assert File.get_by_id(again.id).is_deleted is False
    client.raise_on.pop("delete_object", None)

    inside = [make_file(repo, "folder/a.txt", "a", size=1), make_file(repo, "folder/b.txt", "b", size=1)]
    # A folder whose name is a prefix of another must not be taken with it
    sibling = make_file(repo, "folder-old/c.txt", "c", size=1)
    client.list_payload = {
        "results": [
            {"path_type": "object", "path": "folder/a.txt"},
            {"path_type": "object", "path": "folder/b.txt"},
            {"path_type": "common_prefix", "path": "folder/sub/"},
        ]
    }
    folder_deleted = await commit_ops.process_deleted_folder("folder", repo, "lakefs", "main")
    assert folder_deleted is True
    assert [File.get_by_id(row.id).is_deleted for row in inside] == [True, True]
    assert File.get_by_id(sibling.id).is_deleted is False

    client.raise_on["list_objects"] = lakefs_error(500, "list failed")
    with pytest.raises(HTTPException) as listing_failed:
        await commit_ops.process_deleted_folder("folder", repo, "lakefs", "main")
    assert listing_failed.value.status_code == 500


@pytest.mark.asyncio
async def test_process_copy_file_covers_validation_success_and_error(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops, "should_use_lfs", lambda repo_arg, path, size: True)
    make_file(repo, "src.txt", "abc", size=12, lfs=True)

    with pytest.raises(HTTPException) as missing_src:
        await commit_ops.process_copy_file("dest.txt", None, "main", repo, "lakefs", "main")
    assert missing_src.value.status_code == 400

    copied = await commit_ops.process_copy_file("dest.txt", "src.txt", "main", repo, "lakefs", "main")
    assert copied == (True, None)  # not a global LFS object: nothing to track
    dest = _active(repo, "dest.txt")
    assert dest.lfs is True and dest.sha256 == "abc"

    # The source has no file row: the LakeFS checksum is recorded instead
    copied = await commit_ops.process_copy_file("dest.txt", "lakefs-only.txt", "main", repo, "lakefs", "main")
    assert copied == (True, None)
    assert _active(repo, "dest.txt").sha256 == "sha256:abc"

    # A global LFS object: the linked version's sha256, not the source's
    # current one, claimed and tracked like a linked upload
    old, new = "a" * 64, "b" * 64
    stat = client.stat_object
    client.stat_object = lambda **kwargs: _async_return(
        {"physical_address": f"s3://bucket/lfs/bb/bb/{new}", "checksum": "etag", "size_bytes": 12}
    )
    claims = []
    monkeypatch.setattr(commit_ops, "_claim_lfs_object", lambda oid, key: claims.append(key) or _async_return(None))
    make_file(repo, "linked.txt", old, size=5, lfs=True)
    copied = await commit_ops.process_copy_file("linked.txt", "src.txt", "c0ffee", repo, "lakefs", "main")
    assert copied == (True, {"path": "linked.txt", "sha256": new, "size": 12, "old_sha256": old})
    assert _active(repo, "linked.txt").sha256 == new
    assert claims == [f"lfs/bb/bb/{new}"]

    # A new destination has no previous version
    copied = await commit_ops.process_copy_file("fresh.txt", "src.txt", "c0ffee", repo, "lakefs", "main")
    assert copied[1]["old_sha256"] is None

    def unavailable(oid, key):
        raise HTTPException(409, detail={"error": "upload it again"})

    monkeypatch.setattr(commit_ops, "_claim_lfs_object", unavailable)
    with pytest.raises(HTTPException) as collected:
        await commit_ops.process_copy_file("linked.txt", "src.txt", "c0ffee", repo, "lakefs", "main")
    assert collected.value.status_code == 409
    client.stat_object = stat

    for refusal, status, code in [
        (lakefs_error(503, "copy failed"), 500, "ServerError"),
        (lakefs_error(404, "not found"), 404, "EntryNotFound"),  # the revision was found: the path is what is not there
    ]:
        client.raise_on["stat_object"] = refusal
        with pytest.raises(HTTPException) as copy_error:
            await commit_ops.process_copy_file("dest.txt", "src.txt", "main", repo, "lakefs", "main")
        assert copy_error.value.status_code == status
        assert copy_error.value.headers["X-Error-Code"] == code


@pytest.mark.asyncio
async def test_commit_route_covers_parse_dispatch_noop_and_success_paths(monkeypatch, repo):
    user = SimpleNamespace(username="owner")
    client = _FakeLakeFSClient()
    warnings = []
    tracked = []
    gc_calls = []

    monkeypatch.setattr(commit_ops, "check_repo_write_permission", lambda repo_arg, user_arg: None)
    monkeypatch.setattr(commit_ops, "resolve_lakefs_repo", lambda repo: "model:owner/repo")
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(commit_ops.cfg.app, "debug_log_payloads", False)
    monkeypatch.setattr(commit_ops.cfg.app, "lfs_auto_gc", True)
    monkeypatch.setattr(commit_ops, "process_regular_file", lambda **kwargs: _async_return(False))
    monkeypatch.setattr(commit_ops, "process_lfs_file", lambda **kwargs: _async_return((False, None)))
    monkeypatch.setattr(commit_ops, "process_deleted_file", lambda **kwargs: _async_return(True))
    monkeypatch.setattr(commit_ops, "process_deleted_folder", lambda **kwargs: _async_return(True))
    copied = iter([(True, {"path": "copied.bin", "sha256": "c" * 64, "size": 3, "old_sha256": None})])
    monkeypatch.setattr(
        commit_ops, "process_copy_file", lambda **kwargs: _async_return(next(copied, (True, None)))
    )
    monkeypatch.setattr(commit_ops, "track_lfs_object", lambda **kwargs: tracked.append(kwargs))
    monkeypatch.setattr(
        commit_ops,
        "record_evicted_versions",
        lambda repo_arg, paths: gc_calls.append(list(paths)) or len(gc_calls) % 2,
    )
    collections = []
    monkeypatch.setattr(commit_ops, "enqueue_lfs_collection", lambda: collections.append(1))
    head_changes = []
    monkeypatch.setattr(
        commit_ops,
        "record_head_change",
        lambda repo_arg, branch, paths, folders: head_changes.append((branch, paths, folders)),
    )
    monkeypatch.setattr(commit_ops, "create_commit", lambda **kwargs: tracked.append({"commit": kwargs["commit_id"]}))
    counted = []
    monkeypatch.setattr(
        importlib.import_module("kohakuhub.api.commit.records"),
        "count_main_move",
        lambda client_arg, lakefs_repo, repo_arg, commit_id: _async_return(counted.append(commit_id)),
    )
    monkeypatch.setattr(commit_ops.logger, "warning", lambda message: warnings.append(message))

    # A repository that does not exist is refused before anything else
    with pytest.raises(HTTPException) as missing_repo:
        await commit_ops.commit(commit_ops.RepoType.model, "owner", "missing", "main", _FakeRequest(b""), user=user)
    assert missing_repo.value.status_code == 404

    with pytest.raises(HTTPException) as invalid_json:
        await commit_ops.commit(commit_ops.RepoType.model, "owner", "repo", "main", _FakeRequest(b"{bad"), user=user)
    assert invalid_json.value.status_code == 400

    payload_without_header = json.dumps({"key": "file", "value": {"path": "README.md"}}).encode("utf-8")
    with pytest.raises(HTTPException) as missing_header:
        await commit_ops.commit(commit_ops.RepoType.model, "owner", "repo", "main", _FakeRequest(payload_without_header), user=user)
    assert missing_header.value.status_code == 400

    noop_payload = b"\n".join(
        [
            json.dumps({"key": "header", "value": {"summary": "noop"}}).encode("utf-8"),
            json.dumps({"key": "file", "value": {"path": "README.md", "content": "aGVsbG8=", "encoding": "base64"}}).encode("utf-8"),
        ]
    )
    noop_response = await commit_ops.commit(commit_ops.RepoType.model, "owner", "repo", "main", _FakeRequest(noop_payload), user=user)
    assert noop_response["commitOid"] == "head-commit"
    assert noop_response["commitUrl"] == "models/owner/repo/commit/head-commit"

    monkeypatch.setattr(commit_ops, "process_regular_file", lambda **kwargs: _async_return(True))
    monkeypatch.setattr(
        commit_ops,
        "process_lfs_file",
        lambda **kwargs: _async_return((True, {"path": "weights.bin", "sha256": "oid", "size": 12, "old_sha256": "old"})),
    )
    success_payload = b"\n".join(
        [
            json.dumps({"key": "header", "value": {"summary": "commit", "description": "desc"}}).encode("utf-8"),
            json.dumps({"key": "file", "value": {"path": "README.md", "content": "aGVsbG8=", "encoding": "base64"}}).encode("utf-8"),
            json.dumps({"key": "lfsFile", "value": {"path": "weights.bin", "oid": "oid", "size": 12}}).encode("utf-8"),
            json.dumps({"key": "deletedFile", "value": {"path": "old.txt"}}).encode("utf-8"),
            json.dumps({"key": "deletedFolder", "value": {"path": "folder"}}).encode("utf-8"),
            json.dumps({"key": "copyFile", "value": {"path": "copied.txt", "srcPath": "README.md"}}).encode("utf-8"),
        ]
    )
    success_response = await commit_ops.commit(commit_ops.RepoType.model, "owner", "repo", "main", _FakeRequest(success_payload), user=user)
    assert success_response["commitOid"] == "commit-created"
    assert success_response["commitUrl"] == "models/owner/repo/commit/commit-created"
    assert tracked
    assert counted == ["commit-created"]  # main moved: its usage follows
    assert gc_calls == [["weights.bin"]]  # the path whose old version was replaced
    assert collections == [1]
    # What the branch head links now: LFS results, everything else touched links none
    assert head_changes == [
        (
            "main",
            {
                "README.md": None,
                "weights.bin": "oid",
                "old.txt": None,
                "copied.txt": None,
                "copied.bin": "c" * 64,
            },
            ["folder/"],
        )
    ]

    for refusal, status, code in [
        (lakefs_error(500, "commit failed"), 500, "ServerError"),
        (lakefs_error(404, "branch not found"), 404, "RevisionNotFound"),
        (lakefs_error(409, "conflict"), 409, "BadRequest"),
        (RuntimeError("no answer"), 500, "ServerError"),  # LakeFS did not answer: the commit may have landed
    ]:
        client.raise_on["commit"] = refusal
        with pytest.raises(HTTPException) as commit_failed:
            await commit_ops.commit(commit_ops.RepoType.model, "owner", "repo", "main", _FakeRequest(success_payload), user=user)
        assert commit_failed.value.status_code == status
        assert commit_failed.value.headers["X-Error-Code"] == code

    client.raise_on.pop("commit", None)
    monkeypatch.setattr(commit_ops, "process_lfs_file", lambda **kwargs: _async_return((False, None)))
    success_without_lfs = await commit_ops.commit(commit_ops.RepoType.model, "owner", "repo", "main", _FakeRequest(success_payload), user=user)
    assert success_without_lfs["commitOid"] == "commit-created"
    assert any("No LFS files to track" in message for message in warnings)


def _ndjson(*records):
    return "\n".join(json.dumps(record) for record in records).encode("utf-8")


async def _commit(repo, body, revision="main"):
    return await commit_ops.commit(
        commit_ops.RepoType.model,
        repo.namespace,
        repo.name,
        revision,
        _FakeRequest(body),
        user=repo.owner,
    )


_HEADER = {"key": "header", "value": {"summary": "noop"}}
_README = {"key": "file", "value": {"path": "README.md", "content": "aGVsbG8=", "encoding": "base64"}}


@pytest.fixture
def instant_sleep(monkeypatch):
    """Commit polling waits 0.5 s per attempt: the waits are recorded, not slept."""
    waits = []

    async def record(seconds):
        waits.append(seconds)

    monkeypatch.setattr(commit_ops.asyncio, "sleep", record)
    return waits


@pytest.mark.asyncio
async def test_restore_refused_by_lakefs_keeps_the_file_deleted(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(commit_ops, "object_exists", lambda bucket, key: _async_return(True))
    monkeypatch.setattr(commit_ops, "claim_for_commit", lambda oid, exists: False)
    weights = make_file(repo, "weights.bin", "sameoid", size=10, lfs=True, is_deleted=True)
    client.raise_on["link_physical_address"] = lakefs_error(500, "link refused")

    with pytest.raises(HTTPException) as refused:
        await commit_ops.process_lfs_file("weights.bin", "sameoid", 10, "sha256", repo, "lakefs", "main")

    assert refused.value.status_code == 500
    assert "link refused" in refused.value.detail["error"]
    assert File.get_by_id(weights.id).is_deleted is True  # the row is not touched


@pytest.mark.asyncio
async def test_lfs_object_check_that_s3_cannot_answer_is_a_server_error(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)

    async def unreachable(bucket, key):
        raise EndpointConnectionError(endpoint_url="http://s3.invalid")

    monkeypatch.setattr(commit_ops, "object_exists", unreachable)
    grant_lfs_upload_everywhere("newoid")

    with pytest.raises(HTTPException) as broken:
        await commit_ops.process_lfs_file("weights.bin", "newoid", 11, "sha256", repo, "lakefs", "main")

    assert broken.value.status_code == 500
    assert "Failed to verify LFS object in S3" in broken.value.detail["error"]
    assert not File.select().where((File.repository == repo) & (File.path_in_repo == "weights.bin")).exists()
    assert client.calls == []


@pytest.mark.asyncio
async def test_folder_delete_follows_every_listing_page(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    first = make_file(repo, "pages/a.txt", "a", size=1)
    second = make_file(repo, "pages/b.txt", "b", size=1)
    pages = {
        "": {
            "results": [{"path_type": "object", "path": "pages/a.txt"}],
            "pagination": {"has_more": True, "next_offset": "pages/a.txt"},
        },
        "pages/a.txt": {
            "results": [{"path_type": "object", "path": "pages/b.txt"}],
            "pagination": {"has_more": False},
        },
    }
    listed_after = []

    async def list_objects(**kwargs):
        listed_after.append(kwargs["after"])
        return pages[kwargs["after"]]

    monkeypatch.setattr(client, "list_objects", list_objects)

    assert await commit_ops.process_deleted_folder("pages", repo, "lakefs", "main") is True
    assert listed_after == ["", "pages/a.txt"]
    assert [File.get_by_id(row.id).is_deleted for row in (first, second)] == [True, True]


@pytest.mark.asyncio
async def test_folder_whose_objects_all_refuse_deletion_keeps_its_rows(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    kept = make_file(repo, "stuck/a.txt", "a", size=1)
    client.list_payload = {"results": [{"path_type": "object", "path": "stuck/a.txt"}]}
    client.raise_on["delete_object"] = lakefs_error(500, "LakeFS refuses")

    # It is the commit that fails, not a folder that quietly stays: the rows are not touched
    with pytest.raises(HTTPException) as refused:
        await commit_ops.process_deleted_folder("stuck", repo, "lakefs", "main")
    assert refused.value.status_code == 500 and "LakeFS refuses" in refused.value.detail["error"]
    assert File.get_by_id(kept.id).is_deleted is False


@pytest.mark.asyncio
async def test_debug_payload_logging_writes_each_line(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(commit_ops.cfg.app, "debug_log_payloads", True)
    logged = []
    monkeypatch.setattr(commit_ops.logger, "debug", lambda message: logged.append(message))
    header_line = json.dumps(_HEADER)

    response = await _commit(repo, _ndjson(_HEADER))

    assert response["commitOid"] == "head-commit"
    assert header_line in logged


@pytest.mark.asyncio
async def test_blank_lines_and_unknown_operations_are_skipped(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    body = _ndjson(_HEADER) + b"\n\n" + _ndjson({"key": "mystery", "value": {"path": "ghost.txt"}})

    response = await _commit(repo, body)

    assert response["commitOid"] == "head-commit"  # nothing changed: the head is reported
    assert not File.select().where((File.repository == repo) & (File.path_in_repo == "ghost.txt")).exists()
    assert [call[0] for call in client.calls] == ["get_branch"]


@pytest.mark.asyncio
async def test_a_no_change_commit_reports_the_head_with_the_fields_of_the_hub(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)

    response = await _commit(repo, _ndjson(_HEADER))

    assert response["commitOid"] == "head-commit"
    assert response["commitUrl"] == "models/owner/repo/commit/head-commit"
    assert response["success"] is True and response["hookOutput"] == "" and response["pullRequestUrl"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unreadable,status,code",
    [
        (lakefs_error(503, "branch unreadable"), 500, "ServerError"),
        (lakefs_error(404, "branch not found"), 404, "RevisionNotFound"),
        (httpx.ConnectError("lakefs is down"), 500, "ServerError"),
    ],
)
async def test_a_no_change_commit_whose_head_cannot_be_read_is_an_error_not_a_commit_id(monkeypatch, repo, unreadable, status, code):
    """It was ``200`` with the made-up commit id ``no-changes`` whatever the failure was (#103)."""
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    client.raise_on["get_branch"] = unreadable

    with pytest.raises(HTTPException) as refused:
        await _commit(repo, _ndjson(_HEADER))

    assert refused.value.status_code == status
    assert refused.value.headers["X-Error-Code"] == code
    assert "no-changes" not in json.dumps(refused.value.detail)
    assert isinstance(refused.value.__cause__, httpx.HTTPError)  # raised from what LakeFS did, so the log keeps it


@pytest.mark.asyncio
async def test_a_commit_that_lakefs_refuses_as_unchanged_answers_the_head_or_the_error_of_reading_it(monkeypatch, repo):
    """The second way to a commit with no effective change: the staged content is what the branch holds, and LakeFS says so."""
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    client.raise_on["commit"] = lakefs_error(400, "commit: no changes")

    answered = await _commit(repo, _ndjson(_HEADER, _README))

    assert answered["commitOid"] == "head-commit" and answered["success"] is True
    client.raise_on["get_branch"] = lakefs_error(404, "branch not found")
    with pytest.raises(HTTPException) as unreadable:
        await _commit(repo, _ndjson(_HEADER, _README))
    assert unreadable.value.status_code == 404
    assert unreadable.value.headers["X-Error-Code"] == "RevisionNotFound"


@pytest.mark.asyncio
async def test_commit_polls_until_lakefs_serves_the_commit(monkeypatch, repo, instant_sleep):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    lookups = []

    async def get_commit(**kwargs):
        lookups.append(kwargs["commit_id"])
        if len(lookups) <= 2:
            raise lakefs_error(404, "commit not found")
        return {"id": kwargs["commit_id"]}

    monkeypatch.setattr(client, "get_commit", get_commit)

    # Not main: the main-branch usage count also reads the commit from LakeFS
    response = await _commit(repo, _ndjson(_HEADER, _README), revision="dev")

    assert response["commitOid"] == "commit-created"
    assert len(lookups) == 3
    assert instant_sleep == [0.5, 0.5]


@pytest.mark.asyncio
async def test_commit_stops_polling_after_120_attempts_and_continues(monkeypatch, repo, instant_sleep):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    warnings = []
    monkeypatch.setattr(commit_ops.logger, "warning", lambda message: warnings.append(message))
    lookups = []

    async def never_ready(**kwargs):
        lookups.append(kwargs["commit_id"])
        raise lakefs_error(404, "commit not found")

    monkeypatch.setattr(client, "get_commit", never_ready)

    response = await _commit(repo, _ndjson(_HEADER, _README), revision="dev")

    assert response["commitOid"] == "commit-created"  # the commit stands
    assert len(lookups) == 120
    assert instant_sleep == [0.5] * 119
    assert any("not accessible after 120 attempts" in message for message in warnings)


@pytest.mark.asyncio
async def test_commit_lands_when_its_database_record_fails(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    warnings = []
    monkeypatch.setattr(commit_ops.logger, "warning", lambda message: warnings.append(message))

    # Deliberate database failure, kept as a targeted mock: a real missing table
    # (table_missing) aborts the whole Postgres transaction, so the route's later
    # writes fail too and the test would check the abort rather than this path.
    def failing_record(**kwargs):
        raise RuntimeError("commit table write refused")

    monkeypatch.setattr(commit_ops, "create_commit", failing_record)
    response = await _commit(repo, _ndjson(_HEADER, _README), revision="dev")

    assert response["commitOid"] == "commit-created"
    assert any("Failed to record commit in database" in message for message in warnings)


@pytest.mark.asyncio
async def test_a_folder_delete_that_fails_in_part_settles_every_delete_and_raises_the_first_failure(monkeypatch, repo):
    """The commit is undone as a whole: no row is marked deleted for what was deleted before the failure,
    and every delete has finished by the time it is raised (none runs on after the undo)."""
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    rows = [make_file(repo, f"part/{name}.txt", name, size=1) for name in ("a", "b", "c")]
    client.list_payload = {"results": [{"path_type": "object", "path": f"part/{name}.txt"} for name in ("a", "b", "c")]}
    finished = []

    async def delete_object(**kwargs):
        finished.append(kwargs["path"])
        if kwargs["path"] == "part/b.txt":
            raise lakefs_error(500, "b refused")

    monkeypatch.setattr(client, "delete_object", delete_object)
    with pytest.raises(HTTPException) as caught:
        await commit_ops.process_deleted_folder("part", repo, "lakefs", "main")
    assert caught.value.status_code == 500 and "b refused" in caught.value.detail["error"]
    assert sorted(finished) == ["part/a.txt", "part/b.txt", "part/c.txt"]
    assert [File.get_by_id(row.id).is_deleted for row in rows] == [False, False, False]


@pytest.mark.asyncio
async def test_a_failure_that_is_not_lakefs_is_not_turned_into_a_lakefs_error(monkeypatch, repo):
    """What is not an answer of LakeFS is not mapped as one: it reaches the caller as it is."""
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    client.raise_on["delete_object"] = ValueError("a bug of ours")
    with pytest.raises(ValueError, match="a bug of ours"):
        await commit_ops.process_deleted_file("README.md", repo, "lakefs", "main")


@pytest.mark.asyncio
async def test_a_copy_that_fails_on_our_side_is_a_500_with_its_message(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    make_file(repo, "src.txt", "abc", size=3)
    monkeypatch.setattr(commit_ops, "get_file", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the database is unhappy")))

    with pytest.raises(HTTPException) as caught:
        await commit_ops.process_copy_file("dest.txt", "src.txt", "main", repo, "lakefs", "main")
    assert caught.value.status_code == 500 and "the database is unhappy" in caught.value.detail["error"]


@pytest.mark.asyncio
async def test_a_copy_into_a_branch_that_is_gone_is_a_revision_not_found(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    make_file(repo, "src.txt", "abc", size=3)
    client.raise_on["link_physical_address"] = lakefs_error(404, "branch not found")
    with pytest.raises(HTTPException) as branch_gone:
        await commit_ops.process_copy_file("dest.txt", "src.txt", "main", repo, "lakefs", "main")
    assert branch_gone.value.status_code == 404 and branch_gone.value.headers["X-Error-Code"] == "RevisionNotFound"


@pytest.mark.asyncio
async def test_a_stage_that_fails_in_lakefs_is_undone_and_answered_by_its_status(monkeypatch, repo):
    client = _FakeLakeFSClient()
    monkeypatch.setattr(commit_ops, "get_lakefs_client", lambda: client)
    undone = []

    async def undo(*args, **kwargs):
        undone.append(True)

    monkeypatch.setattr(commit_ops, "_undo", undo)

    async def stage(*args, **kwargs):
        raise lakefs_error(404, "branch not found")

    monkeypatch.setattr(commit_ops, "_stage_operations", stage)
    with pytest.raises(HTTPException) as caught:
        await _commit(repo, _ndjson(_HEADER, _README), revision="dev")
    assert caught.value.status_code == 404 and caught.value.headers["X-Error-Code"] == "RevisionNotFound"
    assert undone == [True]

    async def stage_with_a_bug(*args, **kwargs):
        raise RuntimeError("a bug of ours")

    monkeypatch.setattr(commit_ops, "_stage_operations", stage_with_a_bug)
    with pytest.raises(HTTPException) as bug:
        await _commit(repo, _ndjson(_HEADER, _README), revision="dev")
    assert bug.value.status_code == 500 and "a bug of ours" in bug.value.detail["error"]
    assert undone == [True, True]
