"""A commit, and a preupload, on a revision that is not a branch that is there (#103).

A commit with no operation to a branch that is not there was `200 "no-changes"` while the same commit with one operation
was `404 RevisionNotFound`; and a LakeFS that failed while the head was read was `200 "no-changes"` too, even on a branch
that is there. The Hub answers `RevisionNotFound` for a revision that is not a branch (`create_commit`'s documentation),
and the client sends the files themselves to the store after the preupload, so a preupload on a missing branch let it send
them for nothing. Real LakeFS and database; what real LakeFS cannot be made to do (fail on one call) is injected into the
LakeFS client, as targeted mocks.
"""

import base64
import importlib
import json
import time

import httpx
import pytest

B = "/api/models/owner/demo-model"
ND = {"Content-Type": "application/x-ndjson"}


def _client_class():
    return importlib.import_module("kohakuhub.lakefs_rest_client").LakeFSRestClient


async def _commit(client, ops, revision="main", header=None):
    lines = [{"key": "header", "value": {"summary": "t103", **(header or {})}}] + ops
    body = "\n".join(json.dumps(line) for line in lines) + "\n"
    return await client.post(f"{B}/commit/{revision}", content=body, headers=ND)


def _file(path, text):
    return {"key": "file", "value": {"path": path, "content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}}


async def _head(client, ref="main"):
    return (await client.get(f"{B}/commits/{ref}")).json()[0]["id"]


@pytest.fixture(autouse=True)
async def clean(owner_client):
    yield
    refs = (await owner_client.get(f"{B}/refs")).json()
    for branch in refs["branches"]:
        if branch["name"] != "main":
            await owner_client.delete(f"{B}/branch/{branch['name']}")
    for tag in refs["tags"]:
        await owner_client.delete(f"{B}/tag/{tag['name']}")
    await _commit(owner_client, [{"key": "deletedFolder", "value": {"path": "t103"}}])


@pytest.fixture
async def revisions(owner_client):
    """The revisions that are not a branch that is there."""
    await _commit(owner_client, [_file("t103/a.txt", "one")])
    assert (await owner_client.post(f"{B}/tag", json={"tag": "t103-tag", "revision": "main"})).status_code == 200
    head = await _head(owner_client)
    return {
        "a branch that is not there": "no-such-branch",
        "a tag": "t103-tag",
        "a 40-hex commit id": head,
        "an abbreviation": head[:12],
        "a full ref name": "refs%2Fheads%2Fmain",
    }


NOT_A_REVISION_OPERATION = [
    pytest.param([], id="no operation"),
    pytest.param([{"key": "mystery", "value": {"path": "x"}}], id="only an operation of an unknown kind"),
    pytest.param([_file("t103/n.txt", "new")], id="a new file"),
    pytest.param([{"key": "deletedFile", "value": {"path": "t103/nope.txt"}}], id="a delete"),
]


# ----- a commit


@pytest.mark.parametrize("operations", NOT_A_REVISION_OPERATION)
async def test_a_commit_on_a_revision_that_is_not_a_branch_is_a_404_whatever_it_holds(owner_client, revisions, operations):
    for kind, revision in revisions.items():
        response = await _commit(owner_client, operations, revision)

        assert response.status_code == 404, (kind, response.text)
        assert response.headers["x-error-code"] == "RevisionNotFound", kind
        assert "not found" in response.headers["x-error-message"].lower(), kind


async def test_the_404s_leave_nothing_behind(owner_client, revisions):
    before_commits = len((await owner_client.get(f"{B}/commits/main")).json())
    before_refs = (await owner_client.get(f"{B}/refs")).json()
    started = time.monotonic()

    for revision in revisions.values():
        for _ in range(2):  # a lease the first left behind would make the second wait for the queue (20 s) and answer 429
            assert (await _commit(owner_client, [], revision)).status_code == 404

    assert time.monotonic() - started < 15
    assert len((await owner_client.get(f"{B}/commits/main")).json()) == before_commits
    refs = (await owner_client.get(f"{B}/refs")).json()
    assert [b["name"] for b in refs["branches"]] == [b["name"] for b in before_refs["branches"]]
    assert [t["name"] for t in refs["tags"]] == [t["name"] for t in before_refs["tags"]]


async def test_a_commit_that_changes_nothing_on_a_branch_that_is_there_answers_its_head(owner_client, revisions):
    head = await _head(owner_client)

    empty = await _commit(owner_client, [])
    same = await _commit(owner_client, [_file("t103/a.txt", "one")])

    for response in (empty, same):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["commitOid"] == head and len(head) == 40
        assert body["commitUrl"].endswith(f"/commit/{head}")
    assert await _head(owner_client) == head  # no commit was made


async def test_a_successful_commit_answers_with_the_fields_the_hub_documents(owner_client, revisions):
    """The Hub's schema of the answer: success, commitOid, commitUrl, hookOutput (and pullRequestUrl)."""
    made = await _commit(owner_client, [_file("t103/b.txt", "two")])
    nothing = await _commit(owner_client, [])

    for response in (made, nothing):
        body = response.json()
        assert response.status_code == 200
        assert body["success"] is True
        assert body["hookOutput"] == ""
        assert len(body["commitOid"]) == 40 and body["commitUrl"].endswith(body["commitOid"])
        assert body["pullRequestUrl"] is None
    assert made.json()["commitOid"] == await _head(owner_client)


async def test_who_may_not_write_is_refused_before_the_revision_is_looked_at(owner_client, visitor_client, revisions):
    for revision in ("main", "no-such-branch"):
        response = await _commit(visitor_client, [], revision)

        assert response.status_code == 403, (revision, response.text)


async def test_an_anonymous_visitor_is_refused_before_the_revision_is_looked_at(app, revisions):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anonymous:
        for revision in ("main", "no-such-branch"):
            response = await _commit(anonymous, [], revision)

            assert response.status_code == 401, (revision, response.text)


async def test_parent_commit_and_create_pr_answer_as_before_on_a_missing_branch(owner_client, revisions):
    head = await _head(owner_client)

    with_parent = await _commit(owner_client, [], "no-such-branch", header={"parentCommit": head})
    pr = await owner_client.post(f"{B}/commit/no-such-branch?create_pr=1", content=b"", headers=ND)

    assert with_parent.status_code == 404
    assert pr.status_code == 501


@pytest.mark.parametrize("failure", ["connect", "500", "404"])
async def test_a_head_that_cannot_be_read_is_an_error_never_a_commit_id(owner_client, revisions, monkeypatch, failure):
    """Targeted mock: real LakeFS cannot be made to fail on this one call. Before, all three answered
    200 with the made-up commit id `no-changes`, on a branch that is there."""
    head = await _head(owner_client)
    real = _client_class().get_branch

    async def broken(self, **kwargs):
        if failure == "connect":
            raise httpx.ConnectError("lakefs is down")
        request = httpx.Request("GET", "http://lakefs/branches/main")
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(int(failure), request=request))

    monkeypatch.setattr(_client_class(), "get_branch", broken)
    same = await _commit(owner_client, [_file("t103/a.txt", "one")])
    empty = await _commit(owner_client, [])
    monkeypatch.setattr(_client_class(), "get_branch", real)

    want = 404 if failure == "404" else 500
    for response in (same, empty):
        assert response.status_code == want, response.text
        assert "no-changes" not in response.text
    assert same.headers["x-error-code"] == ("RevisionNotFound" if failure == "404" else "ServerError")
    # nothing is stuck: the same requests work as soon as the head can be read, and a commit with content too
    assert (await _commit(owner_client, [])).json()["commitOid"] == head
    assert (await _commit(owner_client, [_file("t103/c.txt", "three")])).status_code == 200


# ----- the preupload


async def test_a_preupload_on_a_revision_that_resolves_to_nothing_is_a_404(owner_client, revisions):
    files = {"files": [{"path": "t103/p.bin", "size": 5_000_000, "sha256": "0" * 64}]}
    for revision in ("no-such-branch", "refs%2Fheads%2Fno-such", "0" * 40, "f" * 12):
        response = await owner_client.post(f"{B}/preupload/{revision}", json=files)

        assert response.status_code == 404, (revision, response.text)
        assert response.headers["x-error-code"] == "RevisionNotFound", revision
        assert "not found" in response.headers["x-error-message"].lower()


async def test_a_preupload_on_a_tag_or_a_commit_id_that_is_there_answers_as_it_did_for_main(owner_client, revisions):
    """Decided in #12 (``test_commit_id_forms``): the preupload tells what to upload; it is the commit that refuses a revision
    that is not a branch."""
    files = {"files": [{"path": "t103/new.txt", "size": 3, "sample": base64.b64encode(b"new").decode()}]}
    reference = await owner_client.post(f"{B}/preupload/main", json=files)

    for kind in ("a tag", "a 40-hex commit id", "an abbreviation"):
        response = await owner_client.post(f"{B}/preupload/{revisions[kind]}", json=files)

        assert response.status_code == reference.status_code == 200, (kind, response.text)
        assert response.json() == reference.json()


async def test_a_preupload_on_a_branch_that_is_there_answers_as_before(owner_client, revisions):
    files = {"files": [{"path": "t103/a.txt", "size": 3, "sha256": "0" * 64}, {"path": "t103/big.bin", "size": 50_000_000, "sha256": "1" * 64}]}

    response = await owner_client.post(f"{B}/preupload/main", json=files)

    assert response.status_code == 200, response.text
    assert [(f["path"], f["uploadMode"]) for f in response.json()["files"]] == [("t103/a.txt", "regular"), ("t103/big.bin", "lfs")]


async def test_a_preupload_that_cannot_read_the_branch_is_an_error(owner_client, revisions, monkeypatch):
    async def broken(self, **kwargs):
        raise httpx.ConnectError("lakefs is down")

    monkeypatch.setattr(_client_class(), "get_branch", broken)
    response = await owner_client.post(f"{B}/preupload/main", json={"files": [{"path": "t103/a.txt", "size": 3}]})

    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"
