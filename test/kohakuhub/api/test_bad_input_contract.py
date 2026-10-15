"""A request about something that is not there, or that is malformed, is a 4xx: never a 5xx.

A sweep of bad requests (a missing branch, revision, file, repository; an invalid name or path; a
body that is not what it says) found a number of 500s where the Hub answers 404 or 400. Each
request below states the status the Hub answers. The ones the server still gets wrong are marked
``xfail`` with what is wrong: when one is fixed it starts to pass (the report says XPASS) and its mark
goes (the commit operations of #96 did that for three). The rest hold the line, so a fix does not regress and a new route does not
start answering 500 to a client's mistake.

Everything runs against the real database, LakeFS and bucket.
"""

import base64

import pytest

from test.kohakuhub.api.helpers import encode_ndjson

REPO = "owner/bad-input-repo"
OTHER = "owner/bad-input-other"
NOTHING = "owner/bad-input-nothing"


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _file(path, content="x", *, valid=True):
    """A file operation; ``valid=False`` says "base64" of a body that is not."""
    return {"key": "file", "value": {"path": path, "content": _b64(content) if valid else content, "encoding": "base64"}}


def _commit(*ops, parent=None):
    header = {"summary": "probe", **({"parentCommit": parent} if parent else {})}
    return {"content": encode_ndjson([{"key": "header", "value": header}, *ops]), "headers": {"Content-Type": "application/x-ndjson"}}


# (id, method, path, request keywords, the status the Hub answers, why the server is wrong or None)
PROBES = [
    ("create_branch from a missing revision", "POST", f"/api/models/{REPO}/branch", {"json": {"branch": "x1", "revision": "nope"}}, 404, None),
    ("create_tag on a missing revision", "POST", f"/api/models/{REPO}/tag", {"json": {"tag": "t1", "revision": "nope"}}, 404, None),
    ("create_branch with an invalid name", "POST", f"/api/models/{REPO}/branch", {"json": {"branch": "a..b", "revision": "main"}}, 400, None),
    ("create_tag with an invalid name", "POST", f"/api/models/{REPO}/tag", {"json": {"tag": "bad name", "revision": "main"}}, 400, None),
    ("delete a branch that is not there", "DELETE", f"/api/models/{REPO}/branch/never", {}, 404, None),
    ("delete a tag that is not there", "DELETE", f"/api/models/{REPO}/tag/never", {}, 404, None),
    ("commit to a missing branch", "POST", f"/api/models/{REPO}/commit/nope", _commit(_file("b.txt")), 404, None),
    ("commit deleting a missing file", "POST", f"/api/models/{REPO}/commit/main", _commit({"key": "deletedFile", "value": {"path": "nope.txt"}}), 200, None),
    ("commit deleting a missing folder", "POST", f"/api/models/{REPO}/commit/main", _commit({"key": "deletedFolder", "value": {"path": "nope/"}}), 200, None),
    ("commit copying a missing file", "POST", f"/api/models/{REPO}/commit/main", _commit({"key": "copyFile", "value": {"path": "c.txt", "srcPath": "nope.txt", "srcRevision": "main"}}), 404, None),
    ("commit with a body that is not base64", "POST", f"/api/models/{REPO}/commit/main", _commit(_file("d.txt", "!!!", valid=False)), 400, "the body is not checked: an empty file is stored"),
    ("commit with an LFS file that was never uploaded", "POST", f"/api/models/{REPO}/commit/main", _commit({"key": "lfsFile", "value": {"path": "e.bin", "oid": "0" * 64, "size": 10, "algo": "sha256"}}), 400, None),
    ("commit with the path ..", "POST", f"/api/models/{REPO}/commit/main", _commit(_file("../escape.txt")), 400, "the path is not checked: a tree entry named .. is made"),
    ("commit with an empty path", "POST", f"/api/models/{REPO}/commit/main", _commit(_file("")), 400, None),
    ("commit with no operation", "POST", f"/api/models/{REPO}/commit/main", _commit(), 200, None),
    ("commit on a parent that moved", "POST", f"/api/models/{REPO}/commit/main", _commit(_file("p.txt"), parent="deadbeef"), 412, None),
    ("tree of a missing path", "GET", f"/api/models/{REPO}/tree/main/nope", {}, 404, None),
    ("tree of a missing revision", "GET", f"/api/models/{REPO}/tree/nope", {}, 404, None),
    ("paths-info of a missing path", "POST", f"/api/models/{REPO}/paths-info/main", {"data": {"paths": ["nope.txt"]}}, 200, None),
    ("resolve a missing file", "GET", f"/{REPO}/resolve/main/nope.txt", {}, 404, None),
    ("resolve a missing revision", "GET", f"/{REPO}/resolve/nope/a.txt", {}, 404, None),
    ("list the commits of a missing revision", "GET", f"/api/models/{REPO}/commits/nope", {}, 404, "a LakeFS 404 is a 500 (commit history)"),
    ("info of a missing revision", "GET", f"/api/models/{REPO}/revision/nope", {}, 404, None),
    ("info of a repository whose name is not Latin-1", "GET", "/api/models/owner/模型", {}, 404, None),
    ("refs of a missing repository", "GET", f"/api/models/{NOTHING}/refs", {}, 404, None),
    ("settings of a missing repository", "PUT", f"/api/models/{NOTHING}/settings", {"json": {"private": True}}, 404, None),
    ("delete a missing repository", "DELETE", "/api/repos/delete", {"json": {"type": "model", "name": "bad-input-nothing"}}, 404, None),
    ("move a missing repository", "POST", "/api/repos/move", {"json": {"fromRepo": NOTHING, "toRepo": "owner/bad-input-moved", "type": "model"}}, 404, None),
    ("move onto a name that is taken", "POST", "/api/repos/move", {"json": {"fromRepo": REPO, "toRepo": OTHER, "type": "model"}}, 409, None),
    ("move into another account's namespace", "POST", "/api/repos/move", {"json": {"fromRepo": OTHER, "toRepo": "visitor/bad-input-other", "type": "model"}}, 403, None),
    ("move to a name the Hub refuses", "POST", "/api/repos/move", {"json": {"fromRepo": OTHER, "toRepo": "owner/a--b", "type": "model"}}, 400, None),
    ("create a repository of a type there is not", "POST", "/api/repos/create", {"json": {"type": "banana", "name": "x"}}, 422, None),
    ("create a repository in a namespace that is not there", "POST", "/api/repos/create", {"json": {"type": "model", "name": "x", "organization": "no-such-org-zz"}}, 403, None),
    ("duplicate a missing repository", "POST", f"/api/models/{NOTHING}/duplicate", {"json": {"repository": "owner/x"}}, 404, None),
    ("duplicate to a name the Hub refuses", "POST", f"/api/models/{REPO}/duplicate", {"json": {"repository": "owner/a--b"}}, 400, None),
    ("like a missing repository", "POST", f"/api/models/{NOTHING}/like", {}, 404, None),
    ("unlike what was not liked", "DELETE", f"/api/models/{REPO}/like", {}, 400, None),
    ("merge a missing branch", "POST", f"/api/models/{REPO}/merge/nope/into/main", {"json": {}}, 404, "a LakeFS 404 is a 500 (branches)"),
    ("preupload with a body that is not JSON", "POST", f"/api/models/{REPO}/preupload/main", {"content": b"not json"}, 400, None),
    ("overview of a missing user", "GET", "/api/users/no-such-user-zz/overview", {}, 404, None),
    ("overview of a missing organization", "GET", "/api/organizations/no-such-org-zz/overview", {}, 404, None),
    ("list models with a limit that is not a number", "GET", "/api/models", {"params": {"limit": "abc"}}, 422, None),
    ("list models by a sort there is not", "GET", "/api/models", {"params": {"sort": "zzz"}}, 400, None),
]


@pytest.fixture
async def repos(owner_client):
    """Two repositories, the first with a file (made again, harmlessly, by every probe)."""
    for name in ("bad-input-repo", "bad-input-other"):
        response = await owner_client.post("/api/repos/create", json={"type": "model", "name": name})
        assert response.status_code in (200, 409), response.text
    committed = await owner_client.post(f"/api/models/{REPO}/commit/main", **_commit(_file("a.txt", "hello")))
    assert committed.status_code == 200, committed.text


def _marked(probe):
    name, method, path, kwargs, status, wrong = probe
    marks = [pytest.mark.xfail(reason=wrong, strict=False)] if wrong else []
    return pytest.param(probe, id=name, marks=marks)


@pytest.mark.parametrize("probe", [_marked(p) for p in PROBES])
async def test_bad_input_is_the_hubs_4xx_never_a_5xx(owner_client, repos, probe):
    name, method, path, kwargs, status, wrong = probe
    response = await owner_client.request(method, path, **kwargs)
    assert response.status_code < 500, f"{name}: a client's mistake is a server error: {response.text[:200]}"
    assert response.status_code == status, f"{name}: the Hub answers {status}: {response.text[:200]}"


def test_every_probe_has_a_distinct_name_and_a_status_a_client_can_act_on():
    names = [p[0] for p in PROBES]
    assert len(names) == len(set(names))
    assert all(p[4] < 500 for p in PROBES)
