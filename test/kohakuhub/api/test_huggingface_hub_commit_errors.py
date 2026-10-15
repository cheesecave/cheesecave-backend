"""What `huggingface_hub` sees when LakeFS fails during a commit (#10).

`upload_file`, `delete_file` and `delete_folder` are commits. A branch that is gone is `RevisionNotFoundError`
(the client dispatches on `X-Error-Code`), a conflict is a 409, and a LakeFS that fails is a server error that does not
leave the repository saying something LakeFS does not have. Real LakeFS and MinIO behind the live server; what they
cannot be made to do (a branch deleted in flight, an outage) is injected into the LakeFS client (targeted mocks).
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.utils import HfHubHTTPError, RevisionNotFoundError

from test.kohakuhub.support.fakes import lakefs_error

REPO = "owner/demo-model"
pytestmark = pytest.mark.hf_client


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _client_class():
    return importlib.import_module("kohakuhub.lakefs_rest_client").LakeFSRestClient


@pytest.fixture
async def api(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    yield api
    await _run(api.delete_folder, "t10", repo_id=REPO, commit_message="clean up") if await _run(api.file_exists, REPO, "t10/probe.txt") else None


async def test_a_branch_that_is_gone_while_a_file_is_uploaded_is_revision_not_found(api, monkeypatch):
    async def gone(self, **kwargs):
        raise lakefs_error(404, "branch not found", "POST")

    monkeypatch.setattr(_client_class(), "upload_object", gone)
    with pytest.raises(RevisionNotFoundError):
        await _run(api.upload_file, path_or_fileobj=b"x", path_in_repo="t10/probe.txt", repo_id=REPO, commit_message="gone")


async def test_a_lakefs_that_fails_a_delete_does_not_leave_the_file_deleted_in_the_listing(api, monkeypatch):
    await _run(api.upload_file, path_or_fileobj=b"stay", path_in_repo="t10/probe.txt", repo_id=REPO, commit_message="add")

    async def refuse(self, **kwargs):
        raise lakefs_error(503, "the store is unavailable", "DELETE")

    monkeypatch.setattr(_client_class(), "delete_object", refuse)
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(api.delete_file, "t10/probe.txt", repo_id=REPO, commit_message="delete")
    assert caught.value.response.status_code == 500
    assert "Failed to delete t10/probe.txt" in str(caught.value)  # the user reads why
    monkeypatch.undo()

    assert await _run(api.file_exists, REPO, "t10/probe.txt")  # LakeFS has it, and the listing agrees
    assert [i.path for i in await _run(api.get_paths_info, REPO, ["t10/probe.txt"])] == ["t10/probe.txt"]
    await _run(api.delete_file, "t10/probe.txt", repo_id=REPO, commit_message="delete, now that it works")
    assert not await _run(api.file_exists, REPO, "t10/probe.txt")


async def test_delete_folder_works_and_a_failing_lakefs_does_not_delete_it_half(api, monkeypatch):
    await _run(
        api.create_commit,
        REPO,
        operations=[CommitOperationAdd(f"t10/dir/{name}.txt", name.encode()) for name in ("a", "b", "c")],
        commit_message="three files",
    )
    real = _client_class().delete_object

    async def refuse_b(self, repository, branch, path):
        if path.endswith("b.txt"):
            raise lakefs_error(500, "b refused", "DELETE")
        return await real(self, repository=repository, branch=branch, path=path)

    monkeypatch.setattr(_client_class(), "delete_object", refuse_b)
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(api.delete_folder, "t10/dir", repo_id=REPO, commit_message="half")
    assert caught.value.response.status_code == 500
    monkeypatch.undo()
    assert sorted(await _run(lambda: [i.path for i in api.list_repo_tree(REPO, path_in_repo="t10/dir")])) == [
        "t10/dir/a.txt",
        "t10/dir/b.txt",
        "t10/dir/c.txt",
    ]

    await _run(api.delete_folder, "t10/dir", repo_id=REPO, commit_message="all of it")
    for name in ("a", "b", "c"):
        assert not await _run(api.file_exists, REPO, f"t10/dir/{name}.txt")


async def test_a_conflict_that_lakefs_reports_on_commit_is_a_409_the_client_can_read(api, monkeypatch):
    async def conflict(self, **kwargs):
        raise lakefs_error(409, "the branch changed", "POST")

    monkeypatch.setattr(_client_class(), "commit", conflict)
    with pytest.raises(HfHubHTTPError) as caught:
        await _run(api.upload_file, path_or_fileobj=b"x", path_in_repo="t10/probe.txt", repo_id=REPO, commit_message="conflict")
    assert caught.value.response.status_code == 409
    monkeypatch.undo()
    assert not await _run(api.file_exists, REPO, "t10/probe.txt")
