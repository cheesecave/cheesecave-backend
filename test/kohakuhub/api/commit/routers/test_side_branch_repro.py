"""Reproduction for #11: a commit to a side branch rewrites main's File row."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

import kohakuhub.api.commit.routers.operations as commit_ops
from kohakuhub.api.repo.routers.tree import _build_file_record_map
from kohakuhub.db import File
from test.kohakuhub.support.factories import make_repo, make_user

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
