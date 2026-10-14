"""A commit id in the form of the Hub (40 hex) or an abbreviation takes the same answer as the full one (#12).

The routes that read what the database knows of a commit (its author, its description) looked it up by the id
as given, so a 40 hex id found nothing and the author became LakeFS's committer. Real LakeFS and database.
"""

import pytest

from test.kohakuhub.support.db import history_operations_need_postgres

B = "/api/models/owner/demo-model"
FORMS = [("full", 64), ("hub", 40), ("abbreviated", 12), ("short", 7)]


async def _commit_ids(client):
    return [c["id"] for c in (await client.get(f"{B}/commits/main")).json()]


@pytest.mark.parametrize("name, length", FORMS[1:])
async def test_commit_detail_is_the_same_whatever_the_form(owner_client, name, length):
    commit_id = (await _commit_ids(owner_client))[0]
    full = (await owner_client.get(f"{B}/commit/{commit_id}")).json()
    other = await owner_client.get(f"{B}/commit/{commit_id[:length]}")
    assert other.status_code == 200
    assert other.json() == full
    # what the database knows, not the committer LakeFS names
    assert full["author"] == "owner" and {"user_id", "description", "committed_at"} <= set(full)
    assert full["id"] == full["oid"] == commit_id  # the id answered is always the full one


@pytest.mark.parametrize("name, length", FORMS[1:])
async def test_commit_diff_is_the_same_whatever_the_form(owner_client, name, length):
    commit_id = (await _commit_ids(owner_client))[0]
    full = (await owner_client.get(f"{B}/commit/{commit_id}/diff")).json()
    other = await owner_client.get(f"{B}/commit/{commit_id[:length]}/diff")
    assert other.status_code == 200
    assert other.json() == full
    assert full["author"] == "owner" and full["commit_id"] == commit_id


@pytest.mark.parametrize("name, length", FORMS[1:])
async def test_a_commit_the_database_does_not_know_is_the_same_too(owner_client, name, length):
    """The first commit of a repository is made by LakeFS itself: no row, so the committer it names."""
    commit_id = (await _commit_ids(owner_client))[-1]
    full = (await owner_client.get(f"{B}/commit/{commit_id}")).json()
    other = (await owner_client.get(f"{B}/commit/{commit_id[:length]}")).json()
    assert other == full and other["id"] == commit_id


@pytest.mark.parametrize("path", ["commit/{}", "commit/{}/diff", "commit/{}/operations", "commit/{}/unavailable-files"])
async def test_a_prefix_no_commit_has_is_a_miss_in_every_form(owner_client, path):
    for form in ("f" * 40, "f" * 64, "fffffff"):
        response = await owner_client.get(f"{B}/{path.format(form)}")
        assert response.status_code == 404, (path, form, response.text)


@pytest.mark.parametrize("path", ["commit/{}", "commit/{}/diff"])
async def test_a_miss_on_detail_and_diff_says_revision_not_found_not_a_server_error(owner_client, path):
    """These answered 500: any failure of the lookup was a failure of the server (#10)."""
    for form in ("f" * 40, "f" * 64, "a..b", "nope"):
        response = await owner_client.get(f"{B}/{path.format(form)}")
        assert response.status_code == 404, (path, form, response.text)
        assert response.headers["x-error-code"] == "RevisionNotFound"


# ----- what writes take a commit id: every form does the same -----

import base64  # noqa: E402

ND = {"Content-Type": "application/x-ndjson"}


async def _commit(client, branch, path, parent=None):
    header = {"summary": f"add {path}"}
    if parent is not None:
        header["parentCommit"] = parent
    content = base64.b64encode(path.encode()).decode()
    lines = (
        '{"key":"header","value":%s}\n{"key":"file","value":{"path":"%s","content":"%s","encoding":"base64"}}\n'
        % (__import__("json").dumps(header), path, content)
    )
    return await client.post(f"{B}/commit/{branch}", content=lines, headers=ND)


async def _tree(client, ref):
    return sorted(item["path"] for item in (await client.get(f"{B}/tree/{ref}?recursive=true")).json())


@pytest.mark.parametrize("name, length", FORMS)
async def test_a_commit_on_a_parent_in_any_form_lands_and_a_stale_one_is_refused(owner_client, name, length):
    head = (await owner_client.get(f"{B}/revision/main")).json()["sha"]
    landed = await _commit(owner_client, "main", f"on-{name}.txt", parent=head[:length])
    assert landed.status_code == 200, landed.text
    stale = await _commit(owner_client, "main", f"stale-{name}.txt", parent=head[:length])
    assert stale.status_code == 412
    assert stale.headers["x-error-message"] == "A commit has happened since. Please refresh and try again."


@history_operations_need_postgres  # revert and reset are PostgreSQL only
@pytest.mark.parametrize("name, length", FORMS)
async def test_reset_revert_and_merge_by_a_commit_in_any_form(owner_client, name, length):
    c = owner_client
    # reset: two commits on a branch, back to the first
    branch = f"reset-{name}"
    assert (await c.post(f"{B}/branch/{branch}", json={"revision": "main"})).status_code == 200
    one = (await _commit(c, branch, f"one-{name}.txt")).json()["commitOid"]
    await _commit(c, branch, f"two-{name}.txt")
    reset = await c.post(f"{B}/branch/{branch}/reset", json={"ref": one[:length], "force": True})
    assert reset.status_code == 200, reset.text
    assert [p for p in await _tree(c, branch) if p.startswith(("one-", "two-"))] == [f"one-{name}.txt"]

    # revert: the second of two commits is undone
    branch = f"revert-{name}"
    assert (await c.post(f"{B}/branch/{branch}", json={"revision": "main"})).status_code == 200
    await _commit(c, branch, f"uno-{name}.txt")
    dos = (await _commit(c, branch, f"dos-{name}.txt")).json()["commitOid"]
    revert = await c.post(f"{B}/branch/{branch}/revert", json={"ref": dos[:length]})
    assert revert.status_code == 200, revert.text
    assert [p for p in await _tree(c, branch) if p.startswith(("uno-", "dos-"))] == [f"uno-{name}.txt"]

    # merge: a commit as the source, a branch and a tag started at one
    source = (await _commit(c, f"reset-{name}", f"merge-me-{name}.txt")).json()["commitOid"]
    target = f"merge-{name}"
    assert (await c.post(f"{B}/branch/{target}", json={"revision": "main"})).status_code == 200
    merge = await c.post(f"{B}/merge/{source[:length]}/into/{target}", json={})
    assert merge.status_code == 200, merge.text
    assert f"merge-me-{name}.txt" in await _tree(c, target)
    made = await c.post(f"{B}/branch/from-{name}", json={"startingPoint": source[:length]})
    assert made.status_code == 200, made.text
    assert f"merge-me-{name}.txt" in await _tree(c, f"from-{name}")
    tagged = await c.post(f"{B}/tag/{source[:length]}", json={"tag": f"tag-{name}"})
    assert tagged.status_code == 200, tagged.text
    refs = (await c.get(f"{B}/refs")).json()
    assert {t["name"]: t["targetCommit"] for t in refs["tags"]}[f"tag-{name}"] == source


@pytest.mark.parametrize("path", ["commit/{}", "commit/{}/diff"])
async def test_a_failure_of_lakefs_is_still_a_server_error(owner_client, monkeypatch, path):
    """Only a commit LakeFS does not know is a 404; an outage is not a missing commit."""
    import importlib

    import httpx

    history = importlib.import_module("kohakuhub.api.commit.routers.history")
    real = history.get_lakefs_rest_client()

    class Outage:
        def __getattr__(self, name):
            return getattr(real, name)

        async def get_commit(self, **kwargs):
            request = httpx.Request("GET", "http://lakefs/x")
            raise httpx.HTTPStatusError("unavailable", request=request, response=httpx.Response(503, request=request))

    commit_id = (await _commit_ids(owner_client))[0]
    # Deliberate failure injection: an outage of LakeFS cannot be produced against the real service.
    monkeypatch.setattr(history, "get_lakefs_rest_client", lambda: Outage())
    response = await owner_client.get(f"{B}/{path.format(commit_id)}")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"
