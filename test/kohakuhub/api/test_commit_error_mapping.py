"""The commit route answers a failure of LakeFS by its status, and does not hide one (#10).

Real LakeFS, MinIO and database. What real LakeFS answers is used as it is: a path that is not there is deleted with a
204 (no error), a source path that is not there is a 404. What it cannot be made to answer (a branch deleted while a
commit is in flight, an outage) is injected into the LakeFS client, as targeted mocks.
"""

import base64
import importlib
import json

import pytest

from test.kohakuhub.support.fakes import lakefs_error

B = "/api/models/owner/demo-model"
ND = {"Content-Type": "application/x-ndjson"}


def _client_class():
    return importlib.import_module("kohakuhub.lakefs_rest_client").LakeFSRestClient


async def _commit(client, operations, branch="main", summary="test"):
    lines = [{"key": "header", "value": {"summary": summary}}] + operations
    body = "\n".join(json.dumps(line) for line in lines) + "\n"
    return await client.post(f"{B}/commit/{branch}", content=body, headers=ND)


def _file(path, text):
    return {"key": "file", "value": {"path": path, "content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}}


async def _paths(client, ref="main"):
    items = (await client.get(f"{B}/tree/{ref}?recursive=true")).json()
    return sorted(item["path"] for item in items if item["type"] == "file")  # a recursive tree names its directories too


@pytest.fixture(autouse=True)
async def clean(owner_client):
    yield
    refs = (await owner_client.get(f"{B}/refs")).json()
    for branch in refs["branches"]:
        if branch["name"] != "main":
            await owner_client.delete(f"{B}/branch/{branch['name']}")
    await _commit(owner_client, [{"key": "deletedFolder", "value": {"path": "t10"}}])


# ----- real LakeFS -----


async def test_deleting_a_path_that_is_not_there_is_no_error(owner_client):
    """LakeFS answers 204: the commit goes on, as it did when every failure of the delete was ignored."""
    response = await _commit(owner_client, [{"key": "deletedFile", "value": {"path": "t10/never-was.txt"}}, _file("t10/kept.txt", "kept")])
    assert response.status_code == 200, response.text
    assert "t10/kept.txt" in await _paths(owner_client)


async def test_a_folder_is_deleted_for_real(owner_client):
    assert (await _commit(owner_client, [_file("t10/dir/a.txt", "a"), _file("t10/dir/b.txt", "b"), _file("t10/keep.txt", "k")])).status_code == 200
    assert (await _commit(owner_client, [{"key": "deletedFolder", "value": {"path": "t10/dir"}}])).status_code == 200
    assert [p for p in await _paths(owner_client) if p.startswith("t10/")] == ["t10/keep.txt"]


async def test_copying_a_path_that_is_not_there_is_an_entry_not_found(owner_client):
    response = await _commit(owner_client, [{"key": "copyFile", "value": {"path": "t10/copy.txt", "srcPath": "t10/not-there.txt", "srcRevision": "main"}}])
    assert response.status_code == 404, response.text
    assert response.headers["x-error-code"] == "EntryNotFound"
    assert "t10/not-there.txt" in response.headers["x-error-message"]
    assert "t10/copy.txt" not in await _paths(owner_client)  # nothing of the commit stays


# ----- what a failing LakeFS does to the commit -----


async def test_a_branch_deleted_while_a_commit_stages_is_a_revision_not_found(owner_client, monkeypatch):
    async def gone(self, **kwargs):
        raise lakefs_error(404, "branch not found", "POST")

    monkeypatch.setattr(_client_class(), "upload_object", gone)
    response = await _commit(owner_client, [_file("t10/a.txt", "a")])
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert "Revision 'main' not found" in response.headers["x-error-message"]


async def test_a_lakefs_that_fails_a_delete_fails_the_commit_and_the_file_stays(owner_client, monkeypatch):
    """The file was marked deleted in the database and the commit reported success while LakeFS still had it."""
    assert (await _commit(owner_client, [_file("t10/stay.txt", "stay")])).status_code == 200

    async def refuse(self, **kwargs):
        raise lakefs_error(503, "the store is unavailable", "DELETE")

    monkeypatch.setattr(_client_class(), "delete_object", refuse)
    response = await _commit(owner_client, [{"key": "deletedFile", "value": {"path": "t10/stay.txt"}}])
    assert response.status_code == 500
    assert "Failed to delete t10/stay.txt" in response.headers["x-error-message"]
    monkeypatch.undo()
    assert "t10/stay.txt" in await _paths(owner_client)  # LakeFS has it, and so does the listing
    info = await owner_client.post(f"{B}/paths-info/main", data={"paths": ["t10/stay.txt"]})
    assert [i["path"] for i in info.json()] == ["t10/stay.txt"]


async def test_a_folder_delete_that_fails_in_part_leaves_the_folder_as_it_was(owner_client, monkeypatch):
    assert (await _commit(owner_client, [_file("t10/part/a.txt", "a"), _file("t10/part/b.txt", "b")])).status_code == 200
    real = _client_class().delete_object

    async def refuse_one(self, repository, branch, path):
        if path.endswith("b.txt"):
            raise lakefs_error(500, "b refused", "DELETE")
        return await real(self, repository=repository, branch=branch, path=path)

    monkeypatch.setattr(_client_class(), "delete_object", refuse_one)
    response = await _commit(owner_client, [{"key": "deletedFolder", "value": {"path": "t10/part"}}])
    assert response.status_code == 500 and "b refused" in response.headers["x-error-message"]
    monkeypatch.undo()
    assert [p for p in await _paths(owner_client) if p.startswith("t10/part")] == ["t10/part/a.txt", "t10/part/b.txt"]  # undone as a whole


@pytest.mark.parametrize("status, code", [(409, "BadRequest"), (500, "ServerError"), (404, "RevisionNotFound")])
async def test_the_commit_call_of_lakefs_is_answered_by_its_status(owner_client, monkeypatch, status, code):
    async def refuse(self, **kwargs):
        raise lakefs_error(status, "refused", "POST")

    monkeypatch.setattr(_client_class(), "commit", refuse)
    response = await _commit(owner_client, [_file("t10/c.txt", "c")])
    assert response.status_code == status and response.headers["x-error-code"] == code
    monkeypatch.undo()
    assert "t10/c.txt" not in await _paths(owner_client)  # what it staged is dropped
    assert (await _commit(owner_client, [_file("t10/c.txt", "c")])).status_code == 200  # and the branch takes the next commit
