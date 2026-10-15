"""What the routes decide from the status of a LakeFS error, against real LakeFS (#10).

`is_lakefs_not_found_error` and `is_lakefs_conflict_error` read the status of the answer; the routes that used to match
"404", "409" or "conflict" in the text now rely on LakeFS saying what these tests record: its 404 is one `not found` for
a missing ref and for a missing object alike, a prefix nothing has is an empty 200, and a name that is taken is a 409.
"""

import base64
import importlib
import json

import httpx
import pytest

from test.kohakuhub.support.db import history_operations_need_postgres

B = "/api/models/owner/demo-model"
ND = {"Content-Type": "application/x-ndjson"}


def _lakefs():
    return importlib.import_module("kohakuhub.utils.lakefs")


def _repo_name():
    repo = importlib.import_module("kohakuhub.db_operations").get_repository("model", "owner", "demo-model")
    return _lakefs().resolve_lakefs_repo(repo)


async def _commit(client, branch, path, text):
    content = base64.b64encode(text.encode()).decode()
    body = '{"key":"header","value":%s}\n{"key":"file","value":{"path":"%s","content":"%s","encoding":"base64"}}\n' % (
        json.dumps({"summary": f"write {path}"}),
        path,
        content,
    )
    return await client.post(f"{B}/commit/{branch}", content=body, headers=ND)


@pytest.fixture(autouse=True)
async def only_main(owner_client):
    async def clear():
        for branch in (await owner_client.get(f"{B}/refs")).json()["branches"]:
            if branch["name"] != "main":
                await owner_client.delete(f"{B}/branch/{branch['name']}")

    await clear()
    yield
    await clear()


# ----- what LakeFS answers: the facts the routes rely on -----


async def test_lakefs_says_404_for_a_missing_ref_and_an_object_alike_and_409_for_a_name_taken(owner_client):
    lakefs, client, repository = _lakefs(), _lakefs().get_lakefs_client(), _repo_name()

    with pytest.raises(httpx.HTTPStatusError) as missing_branch:
        await client.get_branch(repository=repository, branch="no-such-branch")
    with pytest.raises(httpx.HTTPStatusError) as missing_object:
        await client.stat_object(repository=repository, ref="main", path="no/such/file.txt")
    with pytest.raises(httpx.HTTPStatusError) as missing_ref_listing:
        await client.list_objects(repository=repository, ref="no-such-branch", prefix="")
    for caught in (missing_branch, missing_object, missing_ref_listing):
        assert lakefs.is_lakefs_not_found_error(caught.value) and not lakefs.is_lakefs_conflict_error(caught.value)
    # the stat of an object and the stat of a ref are the same words: the status is all there is
    assert missing_object.value.response.json() == {"message": "not found"}

    with pytest.raises(httpx.HTTPStatusError) as taken:
        await client.create_branch(repository=repository, name="main", source="main")
    assert lakefs.is_lakefs_conflict_error(taken.value) and not lakefs.is_lakefs_not_found_error(taken.value)


async def test_listing_a_prefix_nothing_has_is_an_empty_answer_not_a_404(owner_client):
    """The premise of the tree route: a 404 of a listing means the ref is gone, never "no such directory"."""
    page = await _lakefs().get_lakefs_client().list_objects(repository=_repo_name(), ref="main", prefix="no/such/dir/")
    assert page["results"] == []


# ----- what the routes do with it -----


async def test_a_branch_that_exists_is_a_conflict_and_the_client_can_read_it(owner_client):
    assert (await owner_client.post(f"{B}/branch/taken", json={})).status_code == 200
    again = await owner_client.post(f"{B}/branch/taken", json={})
    assert again.status_code == 409
    assert again.headers["x-error-code"] == "BadRequest" and "already exists" in again.headers["x-error-message"]


async def test_a_branch_called_conflict_or_404_is_a_branch_not_an_error(owner_client):
    """The words in a name or a URL are not an answer: they must not change what a route decides."""
    for name in ("conflict", "x404x", "not-found", "409"):
        made = await owner_client.post(f"{B}/branch/{name}", json={})
        assert made.status_code == 200, (name, made.text)
        assert (await owner_client.get(f"{B}/revision/{name}")).status_code == 200
        assert (await owner_client.get(f"{B}/tree/{name}")).status_code == 200


@history_operations_need_postgres  # merge is PostgreSQL only
async def test_a_merge_that_conflicts_is_a_409_and_names_the_way_out(owner_client):
    for name in ("left", "right"):
        assert (await owner_client.post(f"{B}/branch/{name}", json={})).status_code == 200
    assert (await _commit(owner_client, "left", "same-file.txt", "from the left")).status_code == 200
    assert (await _commit(owner_client, "right", "same-file.txt", "from the right")).status_code == 200

    merged = await owner_client.post(f"{B}/merge/left/into/right", json={})
    assert merged.status_code == 409, merged.text
    assert "Merge conflict" in merged.text and "source-wins" in merged.text
    resolved = await owner_client.post(f"{B}/merge/left/into/right", json={"strategy": "source-wins"})
    assert resolved.status_code == 200, resolved.text


async def test_the_branch_a_revision_names_is_found_by_the_status(owner_client):
    records = importlib.import_module("kohakuhub.api.commit.records")
    client, repository = _lakefs().get_lakefs_client(), _repo_name()
    assert (await owner_client.post(f"{B}/branch/dev", json={})).status_code == 200
    sha = (await owner_client.get(f"{B}/revision/dev")).json()["sha"]

    assert await records.revision_branch(client, repository, "dev") == "dev"
    assert await records.revision_branch(client, repository, "main") == "main"
    assert await records.revision_branch(client, repository, "no-such-branch") is None  # not a branch: a 404
    assert await records.revision_branch(client, repository, sha) is None  # a commit id is not a branch either
