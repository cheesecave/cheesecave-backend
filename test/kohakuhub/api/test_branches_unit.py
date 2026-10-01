"""Unit tests for branch and tag management helpers and routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.api.branches as branches_api
import kohakuhub.api.operation_capabilities as operation_capabilities


@asynccontextmanager
async def _no_lock(repo):
    yield


@pytest.fixture(autouse=True)
def _repository_not_held(monkeypatch):
    """No history operation holds the fake repositories (the lock itself is
    tested against the real database in test_super_squash.py)."""
    monkeypatch.setattr(branches_api.operation_lock, "ensure_free", lambda repo: None)
    monkeypatch.setattr(branches_api.operation_lock, "writing", _no_lock)


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.branch_data = {"commit_id": "branch-head"}
        self.commit_data = {"id": "commit-1"}
        self.diff_result = {"results": []}
        self.merge_result = {"reference": "merge-commit"}
        self.list_branch_payloads = []
        self.list_tag_payloads = []
        self.raise_on = {}

    def _maybe_raise(self, name):
        error = self.raise_on.get(name)
        if error:
            raise error

    async def get_branch(self, **kwargs):
        self.calls.append(("get_branch", kwargs))
        self._maybe_raise("get_branch")
        return self.branch_data

    async def create_branch(self, **kwargs):
        self.calls.append(("create_branch", kwargs))
        self._maybe_raise("create_branch")
        return {"ok": True}

    async def delete_branch(self, **kwargs):
        self.calls.append(("delete_branch", kwargs))
        self._maybe_raise("delete_branch")
        return {"ok": True}

    async def create_tag(self, **kwargs):
        self.calls.append(("create_tag", kwargs))
        self._maybe_raise("create_tag")
        return {"ok": True}

    async def delete_tag(self, **kwargs):
        self.calls.append(("delete_tag", kwargs))
        self._maybe_raise("delete_tag")
        return {"ok": True}

    async def list_branches(self, **kwargs):
        self.calls.append(("list_branches", kwargs))
        self._maybe_raise("list_branches")
        return self.list_branch_payloads.pop(0)

    async def list_tags(self, **kwargs):
        self.calls.append(("list_tags", kwargs))
        self._maybe_raise("list_tags")
        return self.list_tag_payloads.pop(0)

    async def get_commit(self, **kwargs):
        self.calls.append(("get_commit", kwargs))
        self._maybe_raise("get_commit")
        return self.commit_data

    async def revert_branch(self, **kwargs):
        self.calls.append(("revert_branch", kwargs))
        self._maybe_raise("revert_branch")
        return {"ok": True}

    async def merge_into_branch(self, **kwargs):
        self.calls.append(("merge_into_branch", kwargs))
        self._maybe_raise("merge_into_branch")
        return self.merge_result

    async def diff_refs(self, **kwargs):
        self.calls.append(("diff_refs", kwargs))
        self._maybe_raise("diff_refs")
        return self.diff_result

    async def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self._maybe_raise("delete_object")
        return {"ok": True}

    async def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        self._maybe_raise("get_object")
        return b"content"

    async def upload_object(self, **kwargs):
        self.calls.append(("upload_object", kwargs))
        self._maybe_raise("upload_object")
        return {"ok": True}

    async def commit(self, **kwargs):
        self.calls.append(("commit", kwargs))
        self._maybe_raise("commit")
        return {"id": "new-commit-id"}


def _response_error_message(response) -> str:
    return response.headers.get("x-error-message", "")


@pytest.mark.asyncio
async def test_create_branch_and_tag_routes_cover_success_and_error_paths(monkeypatch):
    repo = SimpleNamespace(repo_type="model", full_id="owner/repo")
    user = SimpleNamespace(username="owner")
    client = _FakeClient()

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: repo)
    monkeypatch.setattr(branches_api, "check_repo_delete_permission", lambda repo_arg, user_arg: None)
    monkeypatch.setattr(branches_api, "resolve_lakefs_repo", lambda repo: f"{repo.repo_type}:{repo.full_id}")
    monkeypatch.setattr(branches_api, "get_lakefs_client", lambda: client)
    recorded = []
    monkeypatch.setattr(
        branches_api, "enqueue_branch_links", lambda repo_arg, branch: recorded.append(branch)
    )

    create_branch_response = await branches_api.create_branch(
        "model",
        "owner",
        "repo",
        branches_api.CreateBranchPayload(branch="feature", revision="dev"),
        user=user,
    )
    create_tag_response = await branches_api.create_tag(
        "model",
        "owner",
        "repo",
        branches_api.CreateTagPayload(tag="v1", revision="dev"),
        user=user,
    )
    compat_branch_response = await branches_api.create_branch_compat(
        "model",
        "owner",
        "repo",
        "feature-2",
        branches_api.CreateBranchCompatPayload(startingPoint="main"),
        user=user,
    )
    compat_tag_response = await branches_api.create_tag_compat(
        "model",
        "owner",
        "repo",
        "main",
        branches_api.CreateTagCompatPayload(tag="v2"),
        user=user,
    )

    assert create_branch_response["success"] is True
    assert create_tag_response["success"] is True
    assert compat_branch_response["success"] is True
    # Both creations record the new branch's LFS links in the background
    assert len(recorded) == 2 and recorded[0] == "feature"
    assert compat_tag_response["success"] is True
    assert ("create_branch", {"repository": "model:owner/repo", "name": "feature", "source": "branch-head"}) in client.calls
    assert ("create_tag", {"repository": "model:owner/repo", "id": "v1", "ref": "branch-head"}) in client.calls

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: None)
    not_found_response = await branches_api.create_branch(
        "model",
        "owner",
        "repo",
        branches_api.CreateBranchPayload(branch="feature"),
        user=user,
    )
    assert not_found_response.status_code == 404

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: repo)
    client.raise_on["create_branch"] = RuntimeError("409 conflict")
    conflict_response = await branches_api.create_branch(
        "model",
        "owner",
        "repo",
        branches_api.CreateBranchPayload(branch="feature"),
        user=user,
    )
    assert conflict_response.status_code == 409
    assert "already exists" in _response_error_message(conflict_response)

    client.raise_on["create_branch"] = RuntimeError("boom")
    generic_branch_error = await branches_api.create_branch(
        "model",
        "owner",
        "repo",
        branches_api.CreateBranchPayload(branch="feature"),
        user=user,
    )
    assert generic_branch_error.status_code == 500

    client.raise_on.pop("create_branch", None)
    client.raise_on["create_tag"] = RuntimeError("tag failed")
    generic_tag_error = await branches_api.create_tag(
        "model",
        "owner",
        "repo",
        branches_api.CreateTagPayload(tag="v3"),
        user=user,
    )
    assert generic_tag_error.status_code == 500


@pytest.mark.asyncio
async def test_delete_branch_and_tag_cover_success_not_found_and_guardrails(monkeypatch):
    repo = SimpleNamespace(repo_type="model", full_id="owner/repo")
    user = SimpleNamespace(username="owner")
    client = _FakeClient()

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: repo)
    monkeypatch.setattr(branches_api, "check_repo_delete_permission", lambda repo_arg, user_arg: None)
    monkeypatch.setattr(branches_api, "resolve_lakefs_repo", lambda repo: f"{repo.repo_type}:{repo.full_id}")
    monkeypatch.setattr(branches_api, "get_lakefs_client", lambda: client)

    main_response = await branches_api.delete_branch("model", "owner", "repo", "main", user=user)
    assert main_response.status_code == 400
    assert "Cannot delete main branch" in _response_error_message(main_response)

    branch_response = await branches_api.delete_branch("model", "owner", "repo", "feature", user=user)
    tag_response = await branches_api.delete_tag("model", "owner", "repo", "v1", user=user)
    assert branch_response["success"] is True
    assert tag_response["success"] is True

    client.raise_on["delete_branch"] = RuntimeError("cannot delete branch")
    branch_error = await branches_api.delete_branch("model", "owner", "repo", "feature", user=user)
    assert branch_error.status_code == 500

    client.raise_on["delete_tag"] = RuntimeError("cannot delete tag")
    tag_error = await branches_api.delete_tag("model", "owner", "repo", "v1", user=user)
    assert tag_error.status_code == 500

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: None)
    not_found = await branches_api.delete_tag("model", "owner", "repo", "v1", user=user)
    assert not_found.status_code == 404


@pytest.mark.asyncio
async def test_reference_helpers_and_list_repo_refs_cover_pagination_and_fallback(monkeypatch):
    repo = SimpleNamespace(repo_type="model", full_id="owner/repo")
    client = _FakeClient()
    warnings = []

    client.list_branch_payloads = [
        {
            "results": [{"id": "dev", "commit_id": "c2"}],
            "pagination": {"has_more": True, "next_offset": "page-2"},
        },
        {"results": [{"name": "main", "commit": {"id": "c1"}}], "pagination": {"has_more": False}},
    ]
    client.list_tag_payloads = [
        [
            {"id": "v2", "hash": "c3"},
            {"name": "v1", "commit": {"commitId": "c0"}},
            {"name": None},
        ]
    ]

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: repo)
    monkeypatch.setattr(branches_api, "check_repo_read_permission", lambda repo_arg, user: None)
    monkeypatch.setattr(branches_api, "resolve_lakefs_repo", lambda repo: f"{repo.repo_type}:{repo.full_id}")
    monkeypatch.setattr(branches_api, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(branches_api.logger, "warning", lambda message: warnings.append(message))

    assert branches_api._resolve_ref_name({"id": "main"}) == "main"
    assert branches_api._resolve_ref_name({"name": "dev"}) == "dev"
    assert branches_api._resolve_target_commit({"commit": {"commit_id": "abc"}}) == "abc"
    assert branches_api._resolve_target_commit({"commitId": "def"}) == "def"

    refs_response = await branches_api.list_repo_refs("model", "owner", "repo", include_prs=True, user=None)
    assert [item["name"] for item in refs_response["branches"]] == ["dev", "main"]
    assert [item["name"] for item in refs_response["tags"]] == ["v1", "v2"]
    assert refs_response["pullRequests"] == []

    failing_client = _FakeClient()
    failing_client.raise_on["list_branches"] = RuntimeError("no branch listing")
    failing_client.raise_on["get_branch"] = RuntimeError("no main")
    failing_client.raise_on["list_tags"] = RuntimeError("no tag listing")
    monkeypatch.setattr(branches_api, "get_lakefs_client", lambda: failing_client)
    fallback_response = await branches_api.list_repo_refs("model", "owner", "repo", user=None)
    assert fallback_response == {"branches": [], "converts": [], "tags": []}
    assert warnings


@pytest.mark.asyncio
async def test_merge_branches_covers_not_found_conflict_success_and_tracking_paths(monkeypatch):
    repo = SimpleNamespace(id=9, repo_type="model", full_id="owner/repo")
    user = SimpleNamespace(username="owner")
    client = _FakeClient()
    created_commits = []

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: repo)
    monkeypatch.setattr(branches_api, "check_repo_write_permission", lambda repo_arg, user_arg: None)
    monkeypatch.setattr(branches_api, "resolve_lakefs_repo", lambda repo: f"{repo.repo_type}:{repo.full_id}")
    monkeypatch.setattr(branches_api, "get_lakefs_client", lambda: client)
    queued = []

    bases = []

    async def changes(client_arg, lakefs_repo, commit_id, base=None):
        bases.append(base)
        return commit_id, {"a.bin": None}

    async def record(client_arg, lakefs_repo, repo_arg, branch, rounds, user_arg, message, description):
        created_commits.append((rounds, message, description))

    monkeypatch.setattr(branches_api.records, "commit_changes", changes)
    monkeypatch.setattr(branches_api.records, "record_commits", record)
    monkeypatch.setattr(branches_api, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    monkeypatch.setattr(branches_api.records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    recounts = []
    monkeypatch.setattr(branches_api.records.usage, "enqueue_repository_recount", recounts.append)

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: None)
    not_found = await branches_api.merge_branches(
        "model", "owner", "repo", "feature", "main", branches_api.MergePayload(), user=user
    )
    assert not_found.status_code == 404

    monkeypatch.setattr(branches_api, "get_repository", lambda *_args: repo)
    client.raise_on["merge_into_branch"] = RuntimeError("merge conflict happened")
    with pytest.raises(HTTPException) as conflict_error:
        await branches_api.merge_branches(
            "model", "owner", "repo", "feature", "main", branches_api.MergePayload(), user=user
        )
    assert conflict_error.value.status_code == 409

    client.raise_on["merge_into_branch"] = RuntimeError("merge broke")
    with pytest.raises(HTTPException) as generic_error:
        await branches_api.merge_branches(
            "model", "owner", "repo", "feature", "main", branches_api.MergePayload(), user=user
        )
    assert generic_error.value.status_code == 500

    client.raise_on.pop("merge_into_branch", None)
    client.merge_result = {"reference": "merge-commit"}
    result = await branches_api.merge_branches(
        "model", "owner", "repo", "feature", "main", branches_api.MergePayload(message="merge it"), user=user
    )
    assert result["result"]["reference"] == "merge-commit"
    assert created_commits[-1] == (
        [("merge-commit", {"a.bin": None})],
        "merge it",
        "Merged feature",
    )
    # A squash (one parent) is measured from the head read before merging;
    # a true merge from its first parent
    head_before = client.branch_data["commit_id"]
    assert bases[-1] == head_before
    client.commit_data = {**client.commit_data, "parents": ["first-parent", "second-parent"]}
    await branches_api.merge_branches(
        "model", "owner", "repo", "feature", "main", branches_api.MergePayload(), user=user
    )
    assert bases[-1] == "first-parent"

    client.merge_result = {"status": "ok"}
    result = await branches_api.merge_branches(
        "model", "owner", "repo", "feature", "main", branches_api.MergePayload(), user=user
    )
    assert result["success"] is True
    assert queued == [1]  # no commit id: the reconciliation records what it did
    assert recounts == [repo.id]  # and main's usage is recounted
    queued.clear()

    async def broken_changes(*args, **kwargs):
        raise RuntimeError("tracking broke")

    # What it changed cannot be read: its commit is still recorded, and the
    # reconciliation records what the branch links
    monkeypatch.setattr(branches_api.records, "commit_changes", broken_changes)
    client.merge_result = {"reference": "merge-commit-2"}
    result = await branches_api.merge_branches(
        "model", "owner", "repo", "feature", "main", branches_api.MergePayload(), user=user
    )
    assert result["success"] is True
    assert created_commits[-1][0] == [("merge-commit-2", {})] and queued == [1]


@pytest.mark.asyncio
async def test_disabled_revert_and_reset_reject_before_repository_lookup(monkeypatch):
    monkeypatch.setattr(
        operation_capabilities.cfg.app, "repository_revert_enabled", False
    )
    monkeypatch.setattr(
        operation_capabilities.cfg.app, "repository_reset_enabled", False
    )

    def unexpected_repository_lookup(*_args):
        raise AssertionError("disabled operation reached repository lookup")

    monkeypatch.setattr(branches_api, "get_repository", unexpected_repository_lookup)
    user = SimpleNamespace(username="owner")

    with pytest.raises(HTTPException) as revert_error:
        await branches_api.revert_branch(
            "model",
            "owner",
            "repo",
            "main",
            branches_api.RevertPayload(ref="abc"),
            user=user,
        )
    assert revert_error.value.status_code == 503
    assert revert_error.value.detail["code"] == "operation_disabled"
    assert revert_error.value.detail["operation"] == "revert"

    with pytest.raises(HTTPException) as reset_error:
        await branches_api.reset_branch(
            "model",
            "owner",
            "repo",
            "main",
            branches_api.ResetPayload(ref="abc", force=True),
            user=user,
        )
    assert reset_error.value.status_code == 503
    assert reset_error.value.detail["code"] == "operation_disabled"
    assert reset_error.value.detail["operation"] == "reset"
