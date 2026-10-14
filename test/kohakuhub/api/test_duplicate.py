"""Duplicating a repository (#68): ``POST /api/{type}s/{ns}/{name}/duplicate``.

Everything runs against the real database, LakeFS and bucket, and
huggingface_hub against a live server. A model arrives as one commit of main's
head; a Space keeps its history under the source's own commit ids.
"""

import asyncio
import base64
import hashlib
import warnings
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from huggingface_hub import HfApi

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _live, lfs
from test.kohakuhub.api.helpers import encode_ndjson
from test.kohakuhub.support.db import history_operations_need_postgres

pytestmark = pytest.mark.usefixtures("commit_ids_in_full")


class Typed(Repo):
    """A repository of any type, built through the API."""

    def __init__(self, m, client, name, repo_type="model"):
        super().__init__(m, client, name)
        self.type, self.id = repo_type, f"owner/{name}"

    async def create(self, **extra):
        body = {"type": self.type, "name": self.name, **extra}
        if self.type == "space":
            body.setdefault("sdk", "gradio")
        response = await self.client.post("/api/repos/create", json=body)
        assert response.status_code == 200, response.text
        return self

    async def commit(self, *ops, branch="main", summary="change"):
        for op in ops:
            if op["key"] == "lfsFile":
                self.put(op["value"].pop("_content"))
        response = await self.client.post(
            f"/api/{self.type}s/{self.id}/commit/{branch}",
            content=encode_ndjson([{"key": "header", "value": {"summary": summary}}, *ops]),
            headers={"Content-Type": "application/x-ndjson"},
        )
        assert response.status_code == 200, response.text
        return response.json()["commitOid"]


@pytest.fixture
def d(prepared_backend_test_state, monkeypatch):
    ns = SimpleNamespace()
    ns.cfg = _live("kohakuhub.config").cfg
    ns.db = _live("kohakuhub.db")
    ns.dup = _live("kohakuhub.api.repo.routers.duplicate")
    ns.sweep = _live("kohakuhub.duplicate_sweep")
    ns.lock = _live("kohakuhub.api.repo.utils.operation_lock")
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    for flag in ("repository_squash_enabled",):
        monkeypatch.setattr(ns.cfg.app, flag, True)
    ns.rest._singleton_client = None
    # The test's own reads: not the singleton, which the live server's loop may bind
    ns.client = ns.rest.LakeFSRestClient(
        endpoint=ns.cfg.lakefs.endpoint,
        access_key=ns.cfg.lakefs.access_key,
        secret_key=ns.cfg.lakefs.secret_key,
    )
    yield ns
    ns.rest._singleton_client = None


def _row(d, full_id, repo_type="model"):
    R = d.db.Repository
    return R.get((R.full_id == full_id) & (R.repo_type == repo_type))


def _gone(d, full_id, repo_type="model"):
    R = d.db.Repository
    return R.get_or_none((R.full_id == full_id) & (R.repo_type == repo_type)) is None


async def _dup(client, repo, to, **body):
    return await client.post(f"/api/{repo.type}s/{repo.id}/duplicate", json={"repository": to, **body})


async def _log(d, row, ref="main"):
    lakefs_repo = d.lakefs.resolve_lakefs_repo(row)
    commits, after = [], ""
    while True:
        page = await d.client.log_commits(repository=lakefs_repo, ref=ref, after=after or None, amount=100)
        commits += page["results"]
        if not page["pagination"]["has_more"]:
            return commits
        after = page["pagination"]["next_offset"]


async def _tree(d, row, ref="main"):
    page = await d.client.list_objects(repository=d.lakefs.resolve_lakefs_repo(row), ref=ref, amount=1000)
    return {o["path"]: (o["checksum"], o["size_bytes"]) for o in page["results"]}


async def _refs(d, row):
    lakefs_repo = d.lakefs.resolve_lakefs_repo(row)
    branches = (await d.client.list_branches(repository=lakefs_repo, amount=100))["results"]
    tags = (await d.client.list_tags(repository=lakefs_repo, amount=100))["results"]
    return sorted(b["id"] for b in branches), sorted(t["id"] for t in tags)


def _files(d, row):
    F = d.db.File
    return {
        (f.path_in_repo, f.sha256, f.size, f.lfs, f.is_deleted)
        for f in F.select().where((F.repository == row) & (F.branch == "main"))
    }


def _sha(data):
    return hashlib.sha256(data).hexdigest()


async def test_a_model_arrives_as_one_commit_of_main_head(d, owner_client, monkeypatch):
    monkeypatch.setattr(d.dup, "PAGE", 2)  # listings page by page
    monkeypatch.setattr(d.dup, "ROWS", 2)  # rows insert batch by batch
    src = await Typed(d, owner_client, "dup-model-src").create()
    v1, v2 = b"weights v1\n" * 80, b"weights v2\n" * 80
    await src.commit(lfs("w.bin", v1), _file("a.txt", "a"), _file("sub/b.txt", "b"), _file("sub/c.txt", "c"), summary="first")
    await src.commit(lfs("w.bin", v2), _delete("a.txt"), summary="second")

    response = await _dup(owner_client, src, "owner/dup-model-dst")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filesCopyPending"] is False
    assert body["url"].endswith("/models/owner/dup-model-dst")

    s_row, d_row = _row(d, src.id), _row(d, "owner/dup-model-dst")
    assert await _tree(d, d_row) == await _tree(d, s_row)
    # One commit, with no parent, of main's head tree
    (commit,) = await _log(d, d_row)
    assert (commit["message"], commit["parents"]) == ("Duplicate of owner/dup-model-src", [])
    assert commit["metadata"] == {"kh_operation": "duplicate", "kh_duplicated": (await _log(d, s_row))[0]["id"]}
    assert commit["meta_range_id"] == (await _log(d, s_row))[0]["meta_range_id"]
    assert await _refs(d, d_row) == (["main"], [])
    # The rows
    assert _files(d, d_row) == _files(d, s_row)
    D = d.db
    commits = list(D.Commit.select().where(D.Commit.repository == d_row))
    assert [(c.commit_id, c.username, c.message) for c in commits] == [
        (commit["id"], "owner", "Duplicate of owner/dup-model-src")
    ]
    heads = lambda row: {(h.branch, h.path_in_repo, h.sha256) for h in D.LfsHeadRef.select().where(D.LfsHeadRef.repository == row)}  # noqa: E731
    assert heads(d_row) == heads(s_row) == {("main", "w.bin", _sha(v2))}
    history = [(h.path_in_repo, h.sha256, h.commit_id) for h in D.LFSObjectHistory.select().where(D.LFSObjectHistory.repository == d_row)]
    assert history == [("w.bin", _sha(v2), commit["id"])]  # the head's version only
    assert {p.commit_id for p in D.PathCommit.select().where(D.PathCommit.repository == d_row)} == {commit["id"]}
    assert {p.path for p in D.PathCommit.select().where(D.PathCommit.repository == d_row)} == {"w.bin", "sub", "sub/b.txt", "sub/c.txt"}
    assert d_row.main_regular_bytes == s_row.main_regular_bytes
    assert (d_row.lfs_bytes, d_row.used_bytes) == (len(v2), len(v2) + d_row.main_regular_bytes)
    assert (d_row.operation, d_row.operation_until) == (None, None)  # released
    # Readable, and the regular files are the copy's own
    for path, content in (("sub/b.txt", b"b"), ("sub/c.txt", b"c")):
        read = await owner_client.get(f"/models/owner/dup-model-dst/resolve/main/{path}")
        assert read.status_code == 302  # to the object's presigned url
        lakefs_repo = d.lakefs.resolve_lakefs_repo(d_row)
        assert await d.client.get_object(repository=lakefs_repo, ref="main", path=path) == content
        stat = await d.client.stat_object(repository=d.lakefs.resolve_lakefs_repo(d_row), ref="main", path=path)
        assert stat["physical_address"].startswith(f"s3://{d.cfg.s3.bucket}/{d.lakefs.resolve_lakefs_repo(d_row)}/")
    # And it takes commits like any repository
    await Typed(d, owner_client, "dup-model-dst").commit(_file("new.txt", "n"))
    assert len(await _log(d, d_row)) == 2


async def test_a_space_keeps_every_commit_under_its_own_id(d, owner_client, monkeypatch):
    monkeypatch.setattr(d.dup, "PAGE", 3)
    src = await Typed(d, owner_client, "dup-space-src", "space").create()
    v1, v2 = b"space weights v1\n" * 60, b"space weights v2\n" * 60
    await src.commit(lfs("w.bin", v1), _file("app.py", "print(1)"), summary="c1")
    await src.commit(lfs("w.bin", v2), _file("README.md", "r"), summary="c2")
    response = await owner_client.post(f"/api/spaces/{src.id}/branch", json={"branch": "side", "revision": "main"})
    assert response.status_code == 200, response.text
    await src.commit(_file("side.txt", "s"), branch="side", summary="on side")
    await src.commit(_file("app.py", "print(3)"), _delete("README.md"), summary="c3")
    response = await owner_client.post(f"/api/spaces/{src.id}/merge/side/into/main", json={})
    assert response.status_code == 200, response.text
    await owner_client.post(f"/api/spaces/{src.id}/tag", json={"tag": "v1", "revision": "main"})

    response = await _dup(owner_client, src, "owner/dup-space-dst")
    assert response.status_code == 200, response.text
    assert response.json()["url"].endswith("/spaces/owner/dup-space-dst")

    s_row, d_row = _row(d, src.id, "space"), _row(d, "owner/dup-space-dst", "space")
    s_log, d_log = await _log(d, s_row), await _log(d, d_row)
    # The same commits (LakeFS orders those of one second as it likes): ids, parents, messages,
    # dates, committers, metadata
    assert d_log[0] == s_log[0]
    assert {c["id"]: c for c in d_log} == {c["id"]: c for c in s_log}
    assert any(len(c["parents"]) == 2 for c in d_log)  # the merge too
    assert await _tree(d, d_row) == await _tree(d, s_row)
    assert await _refs(d, d_row) == (["main"], [])  # branches and tags are not copied
    assert d_row.main_counted_commit == s_log[0]["id"]
    # What the site shows: the source's history, ids as the Hub writes them
    s_list = (await owner_client.get(f"/api/spaces/{src.id}/commits/main")).json()
    d_list = (await owner_client.get("/api/spaces/owner/dup-space-dst/commits/main")).json()
    assert sorted(c["id"] for c in d_list) == sorted(c["id"] for c in s_list)
    D = d.db
    # Rows: the commits (with their authors), every LFS version, the heads, the last commits
    rows = lambda row: {(c.commit_id, c.username, c.message, c.branch) for c in D.Commit.select().where(D.Commit.repository == row)}  # noqa: E731
    on_main = {c["id"] for c in s_log}
    assert rows(d_row) == {r for r in rows(s_row) if r[0] in on_main}
    hist = lambda row: {(h.path_in_repo, h.sha256, h.commit_id) for h in D.LFSObjectHistory.select().where(D.LFSObjectHistory.repository == row)}  # noqa: E731
    assert hist(d_row) == hist(s_row) and {h[1] for h in hist(d_row)} == {_sha(v1), _sha(v2)}
    assert _files(d, d_row) == _files(d, s_row)
    last = lambda row: {p.path: p.commit_id for p in D.PathCommit.select().where((D.PathCommit.repository == row) & (D.PathCommit.branch == "main"))}  # noqa: E731
    assert last(d_row) == last(s_row)
    assert (d_row.main_regular_bytes, d_row.lfs_bytes, d_row.used_bytes) == (s_row.main_regular_bytes, s_row.lfs_bytes, s_row.used_bytes)
    # An old revision reads its old content, from the copy's own storage
    first = s_log[-1]["id"] if s_log[-1]["message"] == "c1" else next(c["id"] for c in s_log if c["message"] == "c1")
    stat = await d.client.stat_object(repository=d.lakefs.resolve_lakefs_repo(d_row), ref=first, path="app.py")
    assert stat["physical_address"].startswith(f"s3://{d.cfg.s3.bucket}/{d.lakefs.resolve_lakefs_repo(d_row)}/")
    assert await d.client.get_object(repository=d.lakefs.resolve_lakefs_repo(d_row), ref=first, path="app.py") == b"print(1)"
    # A copy takes commits, and they are its own
    await Typed(d, owner_client, "dup-space-dst", "space").commit(_file("more.txt", "m"))
    assert len(await _log(d, d_row)) == len(s_log) + 1 and len(await _log(d, s_row)) == len(s_log)


@history_operations_need_postgres
async def test_a_squashed_space_copies_its_history_root(d, owner_client):
    src = await Typed(d, owner_client, "dup-squashed-src", "space").create()
    await src.commit(_file("a.txt", "a"), summary="one")
    await src.commit(_file("b.txt", "b"), summary="two")
    response = await owner_client.post("/api/repos/squash", json={"repo": src.id, "type": "space"})
    assert response.status_code == 200, response.text
    response = await _dup(owner_client, src, "owner/dup-squashed-dst")
    assert response.status_code == 200, response.text
    s_row, d_row = _row(d, src.id, "space"), _row(d, "owner/dup-squashed-dst", "space")
    (commit,) = await _log(d, d_row)
    assert commit == (await _log(d, s_row))[0]
    assert s_row.history_root == commit["id"] and d_row.history_root == s_row.history_root


async def test_the_copy_is_as_private_as_the_original_unless_asked(d, owner_client):
    private = await Typed(d, owner_client, "dup-vis-private").create(private=True)
    public = await Typed(d, owner_client, "dup-vis-public").create()
    cases = [
        (private, {}, True),
        (public, {}, False),
        (private, {"private": False}, False),
        (public, {"private": True}, True),
        (private, {"visibility": "public"}, False),
        (public, {"visibility": "private"}, True),
    ]
    for i, (src, body, want) in enumerate(cases):
        response = await _dup(owner_client, src, f"owner/dup-vis-{i}", **body)
        assert response.status_code == 200, response.text
        assert _row(d, f"owner/dup-vis-{i}").private is want, (src.name, body)
    response = await _dup(owner_client, public, "owner/dup-vis-protected", visibility="protected")
    assert response.status_code == 400 and _gone(d, "owner/dup-vis-protected")


async def test_what_cannot_be_duplicated_says_why(d, owner_client, visitor_client):
    src = await Typed(d, owner_client, "dup-err-src").create()
    await src.commit(_file("a.txt", "a"))
    missing = await owner_client.post("/api/models/owner/dup-err-nothing/duplicate", json={"repository": "owner/x"})
    assert missing.status_code == 404 and missing.headers["x-error-code"] == "RepoNotFound"
    for bad in ("owner", "owner/", "/name", "owner/a/b", ""):
        response = await _dup(owner_client, src, bad)
        assert response.status_code == 400, bad
    # The name is taken: the Hub's 409, with the url a client's exist_ok reads
    assert (await _dup(owner_client, src, "owner/dup-err-dst")).status_code == 200
    again = await _dup(owner_client, src, "owner/dup-err-dst")
    assert again.status_code == 409 and again.json()["url"].endswith("/models/owner/dup-err-dst")
    # Another account's namespace is not for writing; its private repositories are not for reading
    into = await _dup(visitor_client, src, "owner/dup-err-into")
    assert into.status_code == 403 and _gone(d, "owner/dup-err-into")
    private = await Typed(d, owner_client, "dup-err-private").create(private=True)
    seen = await _dup(visitor_client, private, "visitor/dup-err-private")
    assert seen.status_code in (403, 404) and _gone(d, "visitor/dup-err-private")


async def test_a_copy_that_does_not_fit_the_quota_is_refused_before_it_exists(d, owner_client):
    model = await Typed(d, owner_client, "dup-quota-model").create()
    await model.commit(lfs("w.bin", b"q" * 4000), _file("a.txt", "a"))
    space = await Typed(d, owner_client, "dup-quota-space", "space").create()
    await space.commit(lfs("w.bin", b"q" * 4000))
    U = d.db.User
    before = U.get(U.username == "owner").public_quota_bytes
    U.update(public_quota_bytes=1000).where(U.username == "owner").execute()
    try:
        for src, to in ((model, "owner/dup-quota-m2"), (space, "owner/dup-quota-s2")):
            response = await _dup(owner_client, src, to)
            assert response.status_code == 413, response.text
            assert response.json()["detail"]["error"] == "Storage quota exceeded"
            assert _gone(d, to, src.type)
    finally:
        U.update(public_quota_bytes=before).where(U.username == "owner").execute()
    assert (await _dup(owner_client, model, "owner/dup-quota-m2")).status_code == 200


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("boom"),
        HTTPException(409, detail={"error": "the repository is busy"}),
        HTTPException(500, detail="plain text"),
    ],
    ids=["error", "conflict", "text"],
)
async def test_a_failed_copy_is_removed_and_never_answers_409(d, owner_client, monkeypatch, request, failure):
    kind = request.node.callspec.id  # a repository outlives the test that made it
    src = await Typed(d, owner_client, f"dup-fail-{kind}-src").create()
    await src.commit(_file("a.txt", "a"))

    async def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(d.dup, "_store_commits", broken)
    response = await _dup(owner_client, src, f"owner/dup-fail-{kind}-dst")
    # A 409 would read as "the copy exists" to a client's exist_ok
    assert response.status_code == 500, response.text
    assert response.headers["x-error-code"] == "ServerError"
    assert f"Duplicating owner/dup-fail-{kind}-src failed" in response.headers["x-error-message"]
    assert _gone(d, f"owner/dup-fail-{kind}-dst")
    monkeypatch.undo()  # the name is free again
    assert (await _dup(owner_client, src, f"owner/dup-fail-{kind}-dst")).status_code == 200


async def test_a_copy_main_cannot_move_to_is_removed(d, owner_client, monkeypatch):
    src = await Typed(d, owner_client, "dup-reset-src").create()
    await src.commit(_file("a.txt", "a"))

    async def refused(self, **kwargs):
        raise httpx.HTTPStatusError("no", request=httpx.Request("PUT", "http://lakefs"), response=httpx.Response(409))

    monkeypatch.setattr(d.rest.LakeFSRestClient, "hard_reset_branch", refused)
    response = await _dup(owner_client, src, "owner/dup-reset-dst")
    assert response.status_code == 500
    assert "LakeFS would not move main to the copy" in response.headers["x-error-message"]
    assert _gone(d, "owner/dup-reset-dst")


async def test_a_copy_that_vanished_meanwhile_still_answers_500(d, owner_client, monkeypatch):
    src = await Typed(d, owner_client, "dup-vanish-src").create()
    await src.commit(_file("a.txt", "a"))

    async def vanishing(*args, **kwargs):
        d.db.Repository.delete().where(d.db.Repository.full_id == "owner/dup-vanish-dst").execute()
        raise RuntimeError("gone")

    monkeypatch.setattr(d.dup, "_store_commits", vanishing)
    response = await _dup(owner_client, src, "owner/dup-vanish-dst")
    assert response.status_code == 500 and _gone(d, "owner/dup-vanish-dst")


async def test_the_copy_is_held_while_it_is_built(d, owner_client, monkeypatch):
    src = await Typed(d, owner_client, "dup-held-src").create()
    await src.commit(_file("a.txt", "a"))
    seen = {}
    store = d.dup._store_commits

    async def meanwhile(client, dest_lakefs, commits):
        row = _row(d, "owner/dup-held-dst")
        seen["holder"] = d.lock.holder(row.id)
        write = await owner_client.post(
            "/api/models/owner/dup-held-dst/commit/main",
            content=encode_ndjson([{"key": "header", "value": {"summary": "x"}}, _file("x.txt", "x")]),
            headers={"Content-Type": "application/x-ndjson"},
        )
        seen["write"] = write.status_code
        delete = await owner_client.request("DELETE", "/api/repos/delete", json={"type": "model", "name": "dup-held-dst"})
        seen["delete"] = delete.status_code
        await store(client, dest_lakefs, commits)

    monkeypatch.setattr(d.dup, "_store_commits", meanwhile)
    response = await _dup(owner_client, src, "owner/dup-held-dst")
    assert response.status_code == 200, response.text
    assert seen == {"holder": "duplicate", "write": 409, "delete": 409}
    assert _row(d, "owner/dup-held-dst").operation is None


async def test_the_lock_of_a_slow_copy_is_renewed(d, owner_client, monkeypatch):
    repo = await Typed(d, owner_client, "dup-renew").create()
    row = _row(d, repo.id)
    monkeypatch.setattr(d.dup, "RENEW_SECONDS", 0.05)
    async with d.dup._building(row):
        first = d.db.Repository.get_by_id(row.id).operation_until
        await asyncio.sleep(0.4)
        assert d.db.Repository.get_by_id(row.id).operation_until > first
    assert d.db.Repository.get_by_id(row.id).operation is None
    # A lock someone else took stops the renewing: it is no longer ours to extend
    async with d.dup._building(row):
        d.db.Repository.update(operation="other:1").where(d.db.Repository.id == row.id).execute()
        await asyncio.sleep(0.3)
    assert d.db.Repository.get_by_id(row.id).operation == "other:1"  # and the release leaves it alone
    d.db.Repository.update(operation=None, operation_until=None).where(d.db.Repository.id == row.id).execute()
    # A repository already held cannot be built
    token = d.lock.acquire(row.id, "other")
    with pytest.raises(HTTPException) as refused:
        async with d.dup._building(row):
            pass
    assert refused.value.status_code == 409
    d.lock.release(row.id, token)


async def test_a_snapshot_waits_for_another_duplicate_of_the_same_source(d, owner_client, monkeypatch):
    repo = await Typed(d, owner_client, "dup-snap").create()
    row = _row(d, repo.id)

    async def read():
        return "read"

    token = d.lock.acquire(row.id, d.dup.SOURCE_OPERATION)
    asyncio.get_running_loop().call_later(0.3, d.lock.release, row.id, token)
    assert await d.dup._snapshot(row, read) == "read"  # it waited for the holder
    assert d.lock.holder(row.id) is None

    token = d.lock.acquire(row.id, d.dup.SOURCE_OPERATION)
    monkeypatch.setattr(d.dup, "SNAPSHOT_WAIT_SECONDS", 0.3)
    with pytest.raises(HTTPException) as busy:
        await d.dup._snapshot(row, read)
    assert busy.value.status_code == 409
    d.lock.release(row.id, token)


def _status(code):
    return httpx.HTTPStatusError("x", request=httpx.Request("GET", "http://lakefs"), response=httpx.Response(code))


async def test_the_first_read_lakefs_fails_is_tried_again(d, monkeypatch):
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(d.dup.asyncio, "sleep", no_sleep)
    answers = [_status(500), _status(503), "fine"]

    async def flaky():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert await d.dup._retrying(flaky) == "fine" and slept == [0.5, 1.0]

    async def refused():
        raise _status(404)

    with pytest.raises(httpx.HTTPStatusError):  # a 4xx is no race
        await d.dup._retrying(refused)

    calls = []

    async def always():
        calls.append(1)
        raise _status(500)

    with pytest.raises(httpx.HTTPStatusError):  # and it does not go on for ever
        await d.dup._retrying(always)
    assert len(calls) == d.dup.READ_TRIES


async def test_a_repository_a_duplicate_died_in_is_swept(d, owner_client, monkeypatch):
    repos = {}
    for name in ("dead", "recent", "alive", "source", "squash", "idle"):
        repos[name] = await Typed(d, owner_client, f"dup-sweep-{name}").create()
    R, now = d.db.Repository, d.db.utcnow()
    for name, token, until in (
        ("dead", "duplicate:abc", now - timedelta(minutes=10)),
        ("recent", "duplicate:abc", now - timedelta(minutes=1)),  # within the grace
        ("alive", "duplicate:abc", now + timedelta(seconds=30)),  # renewed: still working
        ("source", "duplicate-source:abc", now - timedelta(minutes=10)),  # never a copy
        ("squash", "squash:abc", now - timedelta(minutes=10)),
    ):
        R.update(operation=token, operation_until=until).where(R.full_id == repos[name].id).execute()
    assert [r.full_id for r in d.sweep.leftovers()] == [repos["dead"].id]
    await d.sweep.sweep({})
    assert _gone(d, repos["dead"].id)
    assert all(not _gone(d, repos[n].id) for n in ("recent", "alive", "source", "squash", "idle"))
    assert d.sweep.leftovers() == []


@pytest.mark.hf_client
async def test_hf_api_duplicates_a_model_and_a_space(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    if not hasattr(api, "duplicate_repo"):
        pytest.skip("this huggingface_hub has no duplicate_repo; its duplicate_space is tried below")

    def run():
        api.create_repo("owner/hf-dup-model", exist_ok=True)
        api.upload_file(path_or_fileobj=b"hello", path_in_repo="a.txt", repo_id="owner/hf-dup-model")
        api.duplicate_repo("owner/hf-dup-model", "owner/hf-dup-model-copy")
        copied = api.hf_hub_download("owner/hf-dup-model-copy", "a.txt", force_download=True)
        again = api.duplicate_repo("owner/hf-dup-model", "owner/hf-dup-model-copy", exist_ok=True)  # the 409's url
        return open(copied, "rb").read(), str(again)

    content, again = await asyncio.to_thread(run)
    assert content == b"hello" and again.endswith("owner/hf-dup-model-copy")


@pytest.mark.hf_client
async def test_hf_api_duplicates_a_space_with_its_commit_ids(live_server_url, hf_api_token):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)

    def run():
        api.create_repo("owner/hf-dup-space", repo_type="space", space_sdk="gradio", exist_ok=True)
        api.upload_file(path_or_fileobj=b"one", path_in_repo="app.py", repo_id="owner/hf-dup-space", repo_type="space")
        api.upload_file(path_or_fileobj=b"two", path_in_repo="app.py", repo_id="owner/hf-dup-space", repo_type="space")
        with warnings.catch_warnings():  # duplicate_space is deprecated where duplicate_repo exists
            warnings.simplefilter("ignore")
            if hasattr(api, "duplicate_repo"):
                api.duplicate_repo("owner/hf-dup-space", "owner/hf-dup-space-copy", repo_type="space")
            else:
                api.duplicate_space("owner/hf-dup-space", "owner/hf-dup-space-copy")
        ids = lambda repo: [c.commit_id for c in api.list_repo_commits(repo, repo_type="space")]  # noqa: E731
        return ids("owner/hf-dup-space"), ids("owner/hf-dup-space-copy")

    source, copy = await asyncio.to_thread(run)
    assert copy == source and len(source) == 3
