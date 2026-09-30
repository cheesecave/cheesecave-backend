"""Super Squash in place: a branch's history becomes one commit of its tree.

Everything runs against the real database, LakeFS and bucket, and
huggingface_hub against a live server.
"""

import asyncio
import hashlib
import importlib.util
import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _live, lfs
from test.kohakuhub.api.helpers import encode_ndjson

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "db_migrations"
    / "024_repository_operation_lock.py"
)


@pytest.fixture
def s(prepared_backend_test_state, monkeypatch):
    ns = type("S", (), {})()
    ns.cfg = _live("kohakuhub.config").cfg
    for flag in (
        "repository_squash_enabled",
        "repository_revert_enabled",
        "repository_reset_enabled",
    ):
        monkeypatch.setattr(ns.cfg.app, flag, True)
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.usage = _live("kohakuhub.usage")
    ns.tasks = _live("kohakuhub.tasks")
    ns.cleanup = _live("kohakuhub.storage_cleanup")
    ns.squash = _live("kohakuhub.api.commit.squash")
    ns.lock = _live("kohakuhub.api.repo.utils.operation_lock")
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.rest._singleton_client = None
    # The test's own reads: not the singleton, which the live server's loop may bind
    T = ns.db.BackgroundTask
    T.delete().where(T.kind == ns.cleanup.FORGET_SQUASHED_KIND).execute()
    ns.client = ns.rest.LakeFSRestClient(
        endpoint=ns.cfg.lakefs.endpoint,
        access_key=ns.cfg.lakefs.access_key,
        secret_key=ns.cfg.lakefs.secret_key,
    )
    yield ns
    T.delete().where(T.kind == ns.cleanup.FORGET_SQUASHED_KIND).execute()
    ns.db.LfsObjectTombstone.delete().execute()
    ns.rest._singleton_client = None


async def run_collection(s):
    """Run the LFS collection as the worker would: after the queued branch
    link recordings, past the upload grace period."""
    T = s.db.BackgroundTask
    for task in T.select().where(
        (T.kind == s.cleanup.RECORD_BRANCH_KIND) & (T.status == s.tasks.QUEUED)
    ):
        await s.cleanup.record_branch_links(
            json.loads(task.payload), _live("kohakuhub.task_testing").RecordingContext()
        )
        T.update(status=s.tasks.SUCCEEDED).where(T.id == task.id).execute()
    s.db.LfsRecentObject.update(touched_at=s.db.utcnow() - timedelta(days=2)).execute()
    s.gc.mark_references_reconciled()
    await s.cleanup.collect_lfs({}, _live("kohakuhub.task_testing").RecordingContext())


def _row(s, full_id):
    return s.db.Repository.get(s.db.Repository.full_id == full_id)


async def _new(s, client, name):
    response = await client.post(
        "/api/repos/create", json={"type": "model", "name": name}
    )
    assert response.status_code == 200, response.text
    return Repo(s, client, name)


async def _tree(s, repo, ref="main"):
    page = await s.client.list_objects(
        repository=repo.lakefs_repo, ref=ref, amount=1000
    )
    return {o["path"]: o["checksum"] for o in page["results"]}


async def _log(s, repo, ref="main"):
    return (
        await s.client.log_commits(repository=repo.lakefs_repo, ref=ref, amount=100)
    )["results"]


async def _refs(s, repo):
    branches = (await s.client.list_branches(repository=repo.lakefs_repo, amount=100))[
        "results"
    ]
    tags = (await s.client.list_tags(repository=repo.lakefs_repo, amount=100))[
        "results"
    ]
    return sorted(b["id"] for b in branches), sorted(t["id"] for t in tags)


async def _hf(s, call, **kwargs):
    """Call huggingface_hub against the live server, which runs its own event
    loop: it gets a LakeFS client of its own, and the test's calls theirs."""
    s.rest._singleton_client = None
    try:
        return await asyncio.to_thread(call, **kwargs)
    finally:
        s.rest._singleton_client = None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _stored(s, sha):
    try:
        s.s3.head_object(Bucket=s.cfg.s3.bucket, Key=s.gc.lfs_key(sha))
        return True
    except Exception:
        return False


async def _squash(client, repo, **extra):
    return await client.post(
        "/api/repos/squash", json={"repo": repo.id, "type": "model", **extra}
    )


def _pending(s, kind):
    T = s.db.BackgroundTask
    return [
        json.loads(t.payload)
        for t in T.select().where((T.kind == kind) & (T.status == s.tasks.QUEUED))
    ]


async def test_the_commit_address_is_lakefs_own(s, owner_client):
    """The id computed for a commit is the one LakeFS gives it: plain
    commits, a merge (two parents) and commits with metadata."""
    repo = await _new(s, owner_client, "squash-address")
    await repo.commit(_file("a.txt", "a"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "side", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(_file("b.txt", "b"), branch="side")
    await repo.commit(_file("c.txt", "c"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/merge/side/into/main", json={}
    )
    assert response.status_code == 200, response.text
    commits = await _log(s, repo)
    assert any(len(c["parents"]) == 2 for c in commits) and any(
        c.get("metadata") for c in commits
    )
    for c in commits:
        address = s.squash.commit_address(
            c["committer"],
            c["message"],
            c["meta_range_id"],
            c["creation_date"],
            c.get("metadata") or {},
            c["parents"],
        )
        assert address == c["id"]


async def test_a_repository_squash_keeps_its_tree_in_one_commit(
    s, owner_client, monkeypatch
):
    repo = await _new(s, owner_client, "squash-whole")
    versions = [f"weights v{i}\n".encode() * 50 for i in range(3)]
    for i, data in enumerate(versions):
        await repo.commit(
            lfs("w.bin", data),
            _file("config.json", "{" + "x" * i + "}"),
            _file(f"n{i}.txt", "n"),
        )
    await repo.commit(_delete("n0.txt"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    dev_only = b"only on dev\n" * 30
    await repo.commit(lfs("dev.bin", dev_only), branch="dev")
    response = await owner_client.post(
        f"/api/models/{repo.id}/tag", json={"tag": "v1", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    # Another repository links the first version: it must survive the collection
    other = await _new(s, owner_client, "squash-whole-other")
    await other.commit(lfs("copy.bin", versions[0]))
    before, head = await _tree(s, repo), await repo.head()
    row = _row(s, repo.id)
    regular_before = row.main_regular_bytes

    started = time.monotonic()
    monkeypatch.setattr(
        s.squash, "PAGE", 1
    )  # the branches and tags listed page by page
    response = await _squash(owner_client, repo)
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert time.monotonic() - started < 5
    assert await _tree(s, repo) == before
    (squashed,) = await _log(s, repo)
    assert (squashed["message"], squashed["parents"]) == ("Squash history", [])
    assert squashed["metadata"] == {"kh_operation": "squash", "kh_squashed": head}
    assert await _refs(s, repo) == (["main"], [])
    # What the site shows: one commit, by the owner
    listed = (await owner_client.get(f"/api/models/{repo.id}/commits/main")).json()
    assert [c["id"] for c in listed] == [squashed["id"]]
    D = s.db
    rows = list(D.Commit.select().where(D.Commit.repository == row))
    assert [(c.commit_id, c.username) for c in rows] == [(squashed["id"], "owner")]
    assert {
        r.branch for r in D.LfsHeadRef.select().where(D.LfsHeadRef.repository == row)
    } == {"main"}
    row = _row(s, repo.id)
    assert (row.main_counted_commit, row.main_regular_bytes) == (
        squashed["id"],
        regular_before,
    )

    # The versions only the old history reached are forgotten in the background
    (payload,) = _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    assert payload["commit"] == squashed["id"]
    await s.cleanup.forget_squashed_history(payload)
    H = D.LFSObjectHistory
    left = {(h.path_in_repo, h.sha256) for h in H.select().where(H.repository == row)}
    assert left == {("w.bin", _sha(versions[2]))}
    candidates = {c.sha256 for c in D.LfsGcCandidate.select()}
    assert {_sha(versions[0]), _sha(versions[1]), _sha(dev_only)} <= candidates
    # The dropped branch's files are no longer the repository's
    F = D.File
    active = {
        f.path_in_repo
        for f in F.select().where((F.repository == row) & (F.is_deleted == False))
    }
    assert active == set(before)
    assert _row(s, repo.id).lfs_bytes == len(versions[2])
    await s.cleanup.forget_squashed_history(payload)  # again: nothing more
    assert {
        (h.path_in_repo, h.sha256) for h in H.select().where(H.repository == row)
    } == left

    # Garbage collection removes what nothing relies on any more
    await run_collection(s)
    assert [_stored(s, _sha(v)) for v in versions] == [
        True,
        False,
        True,
    ]  # v0: the other repository
    assert not _stored(s, _sha(dev_only))
    assert _row(s, repo.id).lfs_bytes == len(versions[2])

    # The repository goes on as before
    after = await repo.commit(_file("after.txt", "after"))
    assert [c["id"] for c in await _log(s, repo)] == [after, squashed["id"]]
    assert _row(s, repo.id).main_counted_commit == after
    response = await owner_client.get(
        f"/models/{repo.id}/resolve/main/w.bin", follow_redirects=False
    )
    assert response.status_code in (200, 302, 307), response.text


async def test_huggingface_hub_squashes_one_branch(
    s, owner_client, live_server_url, hf_api_token
):
    repo = await _new(s, owner_client, "squash-branch")
    await repo.commit(_file("a.txt", "1"), lfs("m.bin", b"m1" * 40))
    await repo.commit(_file("a.txt", "2"), lfs("m.bin", b"m2" * 40))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(_file("dev.txt", "d"), branch="dev")
    await repo.commit(_file("dev.txt", "dd"), branch="dev")
    response = await owner_client.post(
        f"/api/models/{repo.id}/tag", json={"tag": "v1", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    main_log, dev_tree = await _log(s, repo), await _tree(s, repo, "dev")
    H = s.db.LFSObjectHistory
    history = H.select().where(H.repository == _row(s, repo.id)).count()
    usage = _row(s, repo.id).used_bytes

    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    await _hf(
        s,
        api.super_squash_history,
        repo_id=repo.id,
        branch="dev",
        commit_message="tidy dev",
    )
    (squashed,) = await _log(s, repo, "dev")
    assert squashed["message"] == "tidy dev" and not squashed["parents"]
    assert await _tree(s, repo, "dev") == dev_tree
    assert await _log(s, repo) == main_log  # other branches and tags are kept
    assert await _refs(s, repo) == (["dev", "main"], ["v1"])
    assert H.select().where(H.repository == _row(s, repo.id)).count() == history
    assert not _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    assert _row(s, repo.id).used_bytes == usage

    # main, with huggingface_hub's default message; dev stays
    head = await repo.head()
    await _hf(s, api.super_squash_history, repo_id=repo.id)
    (squashed,) = await _log(s, repo)
    assert squashed["message"] == "Super-squash branch 'main' using huggingface_hub"
    assert _row(s, repo.id).main_counted_commit == squashed["id"] != head
    assert await _refs(s, repo) == (["dev", "main"], ["v1"])

    # A branch that does not exist, or a tag
    for ref in ("nope", "v1"):
        with pytest.raises(HfHubHTTPError) as error:
            await _hf(s, api.super_squash_history, repo_id=repo.id, branch=ref)
        assert error.value.response.status_code == 404
    response = await owner_client.post(
        f"/api/models/owner/nowhere/super-squash/main", json={}
    )
    assert response.status_code == 404
    response = await owner_client.post(
        f"/api/models/{repo.id}/super-squash/dev"
    )  # no body
    assert response.status_code == 200, response.text
    assert (await _log(s, repo, "dev"))[0]["message"] == "Super-squash branch 'dev'"


async def test_writes_are_refused_while_a_squash_holds_the_repository(
    s, owner_client, admin_client
):
    repo = await _new(s, owner_client, "squash-held")
    first = await repo.commit(_file("a.txt", "a"))
    await repo.commit(_file("a.txt", "b"))
    row = _row(s, repo.id)
    token = s.lock.acquire(row.id, "squash")
    assert token and s.lock.acquire(row.id, "squash") is None
    base = f"/api/models/{repo.id}"
    commit = await owner_client.post(
        f"{base}/commit/main",
        content=encode_ndjson(
            [{"key": "header", "value": {"summary": "x"}}, _file("x.txt", "x")]
        ),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert (
        commit.status_code == 409
        and commit.headers["retry-after"] == s.lock.RETRY_AFTER
    )
    assert commit.json()["detail"]["operation"] == "squash"
    refused = [
        await owner_client.post(
            f"{base}/branch", json={"branch": "b", "revision": "main"}
        ),
        await owner_client.delete(f"{base}/branch/nope"),
        await owner_client.post(f"{base}/tag", json={"tag": "t", "revision": "main"}),
        await owner_client.delete(f"{base}/tag/nope"),
        await owner_client.post(f"{base}/branch/main/revert", json={"ref": first}),
        await owner_client.post(
            f"{base}/branch/main/reset", json={"ref": first, "force": True}
        ),
        await owner_client.post(f"{base}/merge/main/into/main", json={}),
        await owner_client.post(
            "/api/repos/move",
            json={"fromRepo": repo.id, "toRepo": "owner/moved", "type": "model"},
        ),
        await owner_client.request(
            "DELETE", "/api/repos/delete", json={"type": "model", "name": repo.name}
        ),
        await _squash(owner_client, repo),
        await owner_client.post(f"{base}/super-squash/main", json={}),
    ]
    assert [r.status_code for r in refused] == [409] * len(refused)
    assert s.lock.holder(row.id) == "squash"
    s.lock.release(row.id, "squash:someone-else")  # only its holder releases it
    assert s.lock.holder(row.id) == "squash"
    s.lock.release(row.id, token)
    assert s.lock.holder(row.id) is None
    await repo.commit(_file("x.txt", "x"))

    # A holder that died frees the repository once its hold expires
    token = s.lock.acquire(row.id, "squash")
    s.db.Repository.update(operation_until=s.db.utcnow() - timedelta(seconds=1)).where(
        s.db.Repository.id == row.id
    ).execute()
    assert s.lock.holder(row.id) is None
    await repo.commit(_file("y.txt", "y"))
    assert s.lock.acquire(row.id, "squash")
    s.db.Repository.update(operation=None, operation_until=None).where(
        s.db.Repository.id == row.id
    ).execute()


async def test_a_commit_already_uploading_lands_after_the_squash(
    s, owner_client, monkeypatch
):
    """A commit that passed the check before the squash took the repository
    waits before committing; its changes land on top of the squash commit."""
    repo = await _new(s, owner_client, "squash-inflight")
    await repo.commit(_file("a.txt", "a"))
    await repo.commit(_file("a.txt", "b"))
    ops = _live("kohakuhub.api.commit.routers.operations")
    process = ops.process_regular_file
    started = asyncio.Event()

    async def slow(**kwargs):
        result = await process(**kwargs)
        started.set()
        await asyncio.sleep(0.5)  # the squash takes the repository meanwhile
        return result

    monkeypatch.setattr(ops, "process_regular_file", slow)

    async def squash_meanwhile():
        await started.wait()
        return await _squash(owner_client, repo)

    commit, squashed = await asyncio.gather(
        repo.commit(_file("late.txt", "late")), squash_meanwhile()
    )
    assert squashed.status_code == 200, squashed.text
    log = await _log(s, repo)
    assert [c["id"] for c in log][0] == commit and len(log) == 2
    assert log[1]["message"] == "Squash history" and not log[1]["parents"]
    assert "late.txt" in await _tree(s, repo)
    # Forgetting what the squash made unreachable keeps what landed on top
    (payload,) = _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    await s.cleanup.forget_squashed_history(payload)
    F = s.db.File
    late = F.get((F.repository == _row(s, repo.id)) & (F.path_in_repo == "late.txt"))
    assert not late.is_deleted

    # A commit reaching LakeFS while the repository is held waits for it
    started.clear()

    async def hold_briefly():
        await started.wait()
        token = s.lock.acquire(_row(s, repo.id).id, "squash")
        await asyncio.sleep(0.8)
        s.lock.release(_row(s, repo.id).id, token)

    commit, _ = await asyncio.gather(
        repo.commit(_file("waited.txt", "w")), hold_briefly()
    )
    assert (await _log(s, repo))[0]["id"] == commit

    # One that cannot wait that long is refused
    monkeypatch.setattr(s.lock, "WAIT_SECONDS", 0.2)
    started.clear()
    token = None

    async def hold():
        nonlocal token
        await started.wait()
        token = s.lock.acquire(_row(s, repo.id).id, "squash")

    with pytest.raises(AssertionError, match="409"):
        await asyncio.gather(repo.commit(_file("later.txt", "later")), hold())
    s.lock.release(_row(s, repo.id).id, token)


async def test_a_head_moving_before_the_reset_is_squashed_too(
    s, owner_client, monkeypatch
):
    repo = await _new(s, owner_client, "squash-moving")
    await repo.commit(_file("a.txt", "a"))
    record = s.client.create_commit_record
    moved = []

    async def moving(**kwargs):
        await record(**kwargs)
        if not moved:  # a write outside KohakuHub lands in between, once
            await s.client.upload_object(
                repository=repo.lakefs_repo,
                branch="main",
                path="outside.txt",
                content=b"o",
            )
            moved.append(
                (
                    await s.client.commit(
                        repository=repo.lakefs_repo, branch="main", message="outside"
                    )
                )["id"]
            )

    monkeypatch.setattr(
        type(s.client), "create_commit_record", lambda self, **kw: moving(**kw)
    )
    response = await _squash(owner_client, repo)
    assert response.status_code == 200, response.text
    (squashed,) = await _log(s, repo)
    assert squashed["metadata"]["kh_squashed"] == moved[0]
    assert "outside.txt" in await _tree(s, repo)

    # A head that keeps moving is given up on, and changes nothing
    async def always(**kwargs):
        await record(**kwargs)
        await s.client.upload_object(
            repository=repo.lakefs_repo,
            branch="main",
            path="o.txt",
            content=str(time.time()).encode(),
        )
        await s.client.commit(
            repository=repo.lakefs_repo, branch="main", message="outside"
        )

    monkeypatch.setattr(
        type(s.client), "create_commit_record", lambda self, **kw: always(**kw)
    )
    head = await repo.head()
    response = await _squash(owner_client, repo)
    assert (
        response.status_code == 409
        and "kept changing" in response.json()["detail"]["error"]
    )
    assert head in {c["id"] for c in await _log(s, repo)}  # the history is still there
    assert s.lock.holder(_row(s, repo.id).id) is None


async def test_lakefs_refusing_changes_nothing(s, owner_client, monkeypatch):
    repo = await _new(s, owner_client, "squash-refused")
    await repo.commit(_file("a.txt", "a"))
    await repo.commit(_file("a.txt", "b"))
    log = await _log(s, repo)

    async def refused(self, **kwargs):
        request = httpx.Request("POST", "http://lakefs")
        raise httpx.HTTPStatusError(
            "x",
            request=request,
            response=httpx.Response(403, request=request, text="no"),
        )

    monkeypatch.setattr(type(s.client), "create_commit_record", refused)
    response = await _squash(owner_client, repo)
    assert (
        response.status_code == 403
        and "LakeFS refused the squash" in response.json()["detail"]["error"]
    )
    assert await _log(s, repo) == log
    row = _row(s, repo.id)
    assert s.lock.holder(row.id) is None
    assert s.db.Commit.select().where(s.db.Commit.repository == row).count() == 2


async def test_lakefs_failing_to_answer_is_a_server_error(s, owner_client, monkeypatch):
    repo = await _new(s, owner_client, "squash-lakefs-down")

    async def down(self, **kwargs):
        request = httpx.Request("GET", "http://lakefs")
        raise httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(503, request=request)
        )

    monkeypatch.setattr(type(s.client), "get_branch", down)
    with pytest.raises(httpx.HTTPStatusError):  # unhandled: the ASGI client raises it
        await _squash(owner_client, repo)
    assert s.lock.holder(_row(s, repo.id).id) is None


async def test_the_commit_record_is_idempotent(s, owner_client):
    repo = await _new(s, owner_client, "squash-idempotent")
    head = await repo.head()
    metarange = (
        await s.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    )["meta_range_id"]
    fields = dict(
        committer="owner",
        message="m",
        metarange_id=metarange,
        creation_date=1,
        parents=[],
        metadata={},
    )
    commit_id = s.squash.commit_address(**{k: v for k, v in fields.items()})
    for _ in range(2):  # the same content is the same commit
        await s.client.create_commit_record(
            repository=repo.lakefs_repo, commit_id=commit_id, generation=1, **fields
        )
    assert (
        await s.client.get_commit(repository=repo.lakefs_repo, commit_id=commit_id)
    )["id"] == commit_id


async def test_an_admin_squash_and_small_repositories(s, owner_client, admin_client):
    """An admin squashes on the owner's behalf; a repository with no LFS file
    (or only its first commit) has nothing to forget."""
    repo = await _new(s, owner_client, "squash-admin")
    response = await admin_client.post(
        "/api/repos/squash", json={"repo": repo.id, "type": "model", "message": "clean"}
    )
    assert response.status_code == 200, response.text
    (squashed,) = await _log(s, repo)
    assert squashed["message"] == "clean"
    commit = s.db.Commit.get(s.db.Commit.commit_id == squashed["id"])
    assert commit.username == "owner"
    # No LFS history: only the file rows are looked at
    (payload,) = _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    assert payload["through"] == 0
    await s.cleanup.forget_squashed_history(payload)
    await repo.commit(_file("a.txt", "a"))
    assert (await _squash(owner_client, repo)).status_code == 200
    # Bad ids and missing repositories
    assert (
        await owner_client.post(
            "/api/repos/squash", json={"repo": "bad", "type": "model"}
        )
    ).status_code == 400
    assert (
        await owner_client.post(
            "/api/repos/squash", json={"repo": "owner/nope", "type": "model"}
        )
    ).status_code == 404
    # The forget task for a repository deleted meanwhile does nothing
    await s.cleanup.forget_squashed_history(
        {"repo_id": -1, "commit": "x", "through": 1}
    )


async def test_forgetting_pages_through_a_big_tree(s, owner_client, monkeypatch):
    repo = await _new(s, owner_client, "squash-pages")
    await repo.commit(*[lfs(f"f{i}.bin", f"page {i}\n".encode() * 9) for i in range(5)])
    await repo.commit(lfs("f0.bin", b"replaced\n" * 9))
    monkeypatch.setattr(s.cleanup, "LAKEFS_LIST_PAGE", 2)
    assert (await _squash(owner_client, repo)).status_code == 200
    (payload,) = _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    await s.cleanup.forget_squashed_history(payload)
    H = s.db.LFSObjectHistory
    left = {h.sha256 for h in H.select().where(H.repository == _row(s, repo.id))}
    assert _sha(b"page 0\n" * 9) not in left and len(left) == 5


async def test_a_disabled_squash_is_refused(s, owner_client, monkeypatch):
    monkeypatch.setattr(s.cfg.app, "repository_squash_enabled", False)
    response = await owner_client.post(
        "/api/models/owner/demo-model/super-squash/main", json={}
    )
    assert response.status_code == 503


def _migration():
    spec = importlib.util.spec_from_file_location("migration_024", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_adds_the_lock(s):
    migration = _migration()
    D = s.db
    assert migration.is_applied(D.db, s.cfg)
    with D.db.atomic() as transaction:
        for column in ("operation", "operation_until", "history_root"):
            D.db.execute_sql(f'ALTER TABLE "repository" DROP COLUMN "{column}"')
        D.db.execute_sql('DROP TABLE "repository_write"')
        assert not migration.is_applied(D.db, s.cfg)
        assert migration.run() is True
        assert migration.is_applied(D.db, s.cfg)
        assert migration.run() is True  # applied: nothing again
        transaction.rollback()
    # A database with some of it already gets the rest
    with D.db.atomic() as transaction:
        D.db.execute_sql('ALTER TABLE "repository" DROP COLUMN "history_root"')
        D.db.execute_sql('DROP TABLE "repository_write"')
        assert not migration.is_applied(D.db, s.cfg)
        assert migration.run() is True
        assert migration.is_applied(D.db, s.cfg)
        transaction.rollback()
    # Never applied on a schema missing what came before
    with D.db.atomic() as transaction:
        D.db.execute_sql('ALTER TABLE "repository" DROP COLUMN "main_counted_commit"')
        assert not migration.is_applied(D.db, s.cfg)
        transaction.rollback()
    with D.db.atomic() as transaction:
        D.db.execute_sql('DROP TABLE "lfs_gc_state"')
        assert not migration.is_applied(D.db, s.cfg)
        transaction.rollback()


def test_the_migration_reports_a_failure(s, monkeypatch):
    migration = _migration()
    monkeypatch.setattr(migration, "is_applied", lambda db, cfg: False)

    def broken(timestamp_type, serial):
        raise RuntimeError("boom")

    monkeypatch.setattr(migration, "_migrate", broken)
    assert migration.run() is False
    monkeypatch.setattr(
        migration, "should_skip_due_to_future_migrations", lambda *a: True
    )
    assert migration.run() is True


async def _squashed_with_history(s, owner_client, name):
    """A repository squashed after some history: (repo, old commit, squash commit)."""
    repo = await _new(s, owner_client, name)
    old = await repo.commit(_file("a.txt", "old"), lfs("w.bin", b"old weights\n" * 20))
    await repo.commit(_file("a.txt", "new"), lfs("w.bin", b"new weights\n" * 20))
    assert (await _squash(owner_client, repo)).status_code == 200
    return repo, old, (await _log(s, repo))[0]["id"]


async def test_the_history_a_squash_removed_is_gone(
    s, owner_client, live_server_url, hf_api_token
):
    """Nothing reads or restores a commit the squash commit does not reach."""
    repo, old, root = await _squashed_with_history(s, owner_client, "squash-gone")
    after = await repo.commit(_file("b.txt", "after"))
    assert _row(s, repo.id).history_root == root
    base = f"/api/models/{repo.id}"
    gone = {
        "commit": await owner_client.get(f"{base}/commit/{old}"),
        "diff": await owner_client.get(f"{base}/commit/{old}/diff"),
        "commits": await owner_client.get(f"{base}/commits/{old}"),
        "operations": await owner_client.get(f"{base}/commit/{old}/operations"),
        "list operations": await owner_client.post(
            f"{base}/commits/{old}/operations", json={"commit_ids": [old]}
        ),
        "unavailable": await owner_client.get(f"{base}/commit/{old}/unavailable-files"),
        "tree": await owner_client.get(f"{base}/tree/{old}"),
        "paths-info": await owner_client.post(
            f"{base}/paths-info/{old}", data={"paths": ["a.txt"]}
        ),
        "revision": await owner_client.get(f"{base}/revision/{old}"),
        "resolve": await owner_client.get(f"/models/{repo.id}/resolve/{old}/a.txt"),
        "reset": await owner_client.post(
            f"{base}/branch/main/reset", json={"ref": old, "force": True}
        ),
        "revert": await owner_client.post(
            f"{base}/branch/main/revert", json={"ref": old}
        ),
        "merge": await owner_client.post(f"{base}/merge/{old}/into/main", json={}),
        "branch": await owner_client.post(
            f"{base}/branch", json={"branch": "back", "revision": old}
        ),
        "tag": await owner_client.post(
            f"{base}/tag", json={"tag": "back", "revision": old}
        ),
    }
    assert {name: r.status_code for name, r in gone.items()} == {
        name: 404 for name in gone
    }
    assert gone["resolve"].headers["x-error-code"] == "RevisionNotFound"
    # What the squash left is all there: its commit, what came after, branches and tags
    for ref in (root, after, root[:12]):
        r = await owner_client.get(f"/models/{repo.id}/resolve/{ref}/a.txt")
        assert r.status_code in (200, 302, 307), (ref, r.status_code)
    assert (await owner_client.get(f"{base}/commit/{root}")).status_code == 200
    response = await owner_client.post(
        f"{base}/branch", json={"branch": "kept", "revision": root}
    )
    assert response.status_code == 200, response.text
    response = await owner_client.post(
        f"{base}/tag", json={"tag": "t1", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    assert (
        await owner_client.get(f"/models/{repo.id}/resolve/t1/a.txt")
    ).status_code in (200, 302, 307)
    # huggingface_hub sees the old revision as missing
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import RevisionNotFoundError

    with pytest.raises(RevisionNotFoundError):
        await _hf(
            s,
            hf_hub_download,
            repo_id=repo.id,
            filename="a.txt",
            revision=old,
            endpoint=live_server_url,
            token=hf_api_token,
            cache_dir=f"/tmp/hf-squash-{time.time()}",
        )


async def test_what_decides_the_history(s, owner_client, monkeypatch):
    repo, old, root = await _squashed_with_history(s, owner_client, "squash-decides")
    lakefs = _live("kohakuhub.utils.lakefs")
    row = _row(s, repo.id)
    # Remembered: a commit is asked about once
    calls = []
    find = s.client.find_merge_base

    async def counted(**kwargs):
        calls.append(kwargs)
        return await find(**kwargs)

    monkeypatch.setattr(s.client, "find_merge_base", counted)
    lakefs._descends.clear()
    assert not await lakefs.in_history(s.client, row, repo.lakefs_repo, old)
    assert not await lakefs.in_history(s.client, row, repo.lakefs_repo, old)
    assert len(calls) == 1
    monkeypatch.setattr(lakefs, "DESCENDS_KEPT", 1)  # full: forgotten, asked again
    after = await repo.commit(_file("c.txt", "c"))
    assert await lakefs.in_history(s.client, row, repo.lakefs_repo, after)
    assert len(lakefs._descends) == 1
    # A repository never squashed, or its root itself, asks nothing
    other = await _new(s, owner_client, "squash-decides-other")
    assert await lakefs.ref_in_history(
        s.client, _row(s, other.id), other.lakefs_repo, "whatever"
    )
    assert await lakefs.in_history(s.client, row, repo.lakefs_repo, root)

    # LakeFS failing otherwise surfaces
    def failing(status):
        async def call(**kwargs):
            request = httpx.Request("GET", "http://lakefs")
            raise httpx.HTTPStatusError(
                "x", request=request, response=httpx.Response(status, request=request)
            )

        return call

    monkeypatch.setattr(s.client, "get_branch", failing(503))
    with pytest.raises(httpx.HTTPStatusError):
        await lakefs.ref_in_history(s.client, row, repo.lakefs_repo, old)
    monkeypatch.setattr(s.client, "get_branch", failing(404))
    monkeypatch.setattr(s.client, "get_commit", failing(503))
    with pytest.raises(httpx.HTTPStatusError):
        await lakefs.ref_in_history(s.client, row, repo.lakefs_repo, old)
    monkeypatch.setattr(s.client, "get_commit", failing(404))
    assert await lakefs.ref_in_history(
        s.client, row, repo.lakefs_repo, "unknown"
    )  # the caller says 404
    monkeypatch.undo()
    with pytest.raises(
        httpx.HTTPStatusError
    ):  # a merge base LakeFS cannot compute for another reason
        await s.client.find_merge_base(
            repository=repo.lakefs_repo, left="nope", right=root
        )


async def test_a_squash_waits_for_writes_under_way(s, owner_client, monkeypatch):
    repo = await _new(s, owner_client, "squash-drain")
    await repo.commit(_file("a.txt", "a"))
    row = _row(s, repo.id)
    W = s.db.RepositoryWrite
    write = W.create(repository=row.id, until=s.db.utcnow() + timedelta(minutes=5))

    async def finish_later():
        await asyncio.sleep(0.6)
        write.delete_instance()

    started = time.monotonic()
    response, _ = await asyncio.gather(_squash(owner_client, repo), finish_later())
    assert response.status_code == 200 and time.monotonic() - started >= 0.5
    # A registration whose writer died lapses; one that stays blocks, then refuses
    W.create(repository=row.id, until=s.db.utcnow() - timedelta(seconds=1))
    await repo.commit(_file("b.txt", "b"))
    assert (await _squash(owner_client, repo)).status_code == 200
    stuck = W.create(repository=row.id, until=s.db.utcnow() + timedelta(minutes=5))
    monkeypatch.setattr(s.lock, "DRAIN_SECONDS", 0.3)
    response = await _squash(owner_client, repo)
    assert (
        response.status_code == 409
        and "writes before squash" in response.json()["detail"]["error"]
    )
    assert s.lock.holder(row.id) is None
    stuck.delete_instance()


async def test_the_lock_is_renewed_and_a_partial_squash_can_be_finished(
    s, owner_client, monkeypatch
):
    repo = await _new(s, owner_client, "squash-partial")
    await repo.commit(_file("a.txt", "a"), lfs("d.bin", b"dropped\n" * 20))
    for name in ("b1", "b2"):
        response = await owner_client.post(
            f"/api/models/{repo.id}/branch", json={"branch": name, "revision": "main"}
        )
        assert response.status_code == 200, response.text
    await repo.commit(lfs("only-b1.bin", b"only on b1\n" * 20), branch="b1")
    response = await owner_client.post(
        f"/api/models/{repo.id}/tag", json={"tag": "v1", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    row = _row(s, repo.id)
    renewed = []
    renew = s.lock.renew

    def counting(repo_id, token):
        renewed.append(renew(repo_id, token))
        return renewed[-1]

    monkeypatch.setattr(s.lock, "renew", counting)

    async def refused(self, **kwargs):
        request = httpx.Request("DELETE", "http://lakefs")
        raise httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(500, request=request)
        )

    delete_tag = type(s.client).delete_tag
    monkeypatch.setattr(type(s.client), "delete_tag", refused)
    response = await _squash(owner_client, repo)
    assert response.status_code == 502, response.text
    assert "squash again to finish" in response.json()["detail"]["error"]
    assert renewed == [True, True]  # after each branch
    assert len(await _log(s, repo)) == 1 and await _refs(s, repo) == (["main"], ["v1"])
    # The dropped branch's objects were handed to the collection; the history stays for now
    assert _sha(b"only on b1\n" * 20) in {
        c.sha256 for c in s.db.LfsGcCandidate.select()
    }
    assert _row(s, repo.id).history_root is None and not _pending(
        s, s.cleanup.FORGET_SQUASHED_KIND
    )
    monkeypatch.setattr(type(s.client), "delete_tag", delete_tag)
    assert (await _squash(owner_client, repo)).status_code == 200
    assert await _refs(s, repo) == (["main"], [])
    assert _row(s, repo.id).history_root and _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    assert not s.lock.renew(row.id, "squash:not-the-holder")


async def test_the_old_regular_objects_are_deleted(s, owner_client, monkeypatch):
    """Only what the old history had goes: what the squash commit, a branch
    made afterwards, a commit afterwards and a staged upload link stays."""
    repo = await _new(s, owner_client, "squash-purge")
    await repo.commit(_file("a.txt", "a1"), _file("gone.txt", "g"))
    await repo.commit(_file("a.txt", "a2"), _delete("gone.txt"))
    old_objects = set()
    for commit in await _log(s, repo):
        page = await s.client.list_objects(
            repository=repo.lakefs_repo, ref=commit["id"], amount=100
        )
        old_objects |= {o["physical_address"] for o in page["results"]}
    assert (await _squash(owner_client, repo)).status_code == 200
    (payload,) = _pending(s, s.cleanup.FORGET_SQUASHED_KIND)
    task = s.db.BackgroundTask.get(
        s.db.BackgroundTask.kind == s.cleanup.FORGET_SQUASHED_KIND
    )
    assert task.run_after > s.db.utcnow() + timedelta(
        minutes=4
    )  # after the uploads under way
    current = {
        o["physical_address"]
        for o in (
            await s.client.list_objects(
                repository=repo.lakefs_repo, ref="main", amount=100
            )
        )["results"]
    }
    after = await repo.commit(_file("after.txt", "after"))
    await s.client.upload_object(
        repository=repo.lakefs_repo, branch="main", path="staged.txt", content=b"staged"
    )
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "later", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    monkeypatch.setattr(
        s.cleanup, "LAKEFS_LIST_PAGE", 1
    )  # branches and trees page by page
    monkeypatch.setattr(s.cleanup, "S3_DELETE_BATCH", 1)  # the bucket too
    await s.cleanup.forget_squashed_history(payload)

    def stored(address):
        bucket, _, key = address.removeprefix("s3://").partition("/")
        try:
            s.s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    assert all(stored(a) for a in current)
    assert not any(stored(a) for a in old_objects - current)
    tree = {
        o["path"]: o["physical_address"]
        for o in (
            await s.client.list_objects(
                repository=repo.lakefs_repo, ref="main", amount=100
            )
        )["results"]
    }
    assert all(stored(tree[p]) for p in ("after.txt", "staged.txt", "a.txt"))
    assert (
        await owner_client.get(f"/models/{repo.id}/resolve/main/a.txt")
    ).status_code in (200, 302, 307)
    await s.cleanup.forget_squashed_history(payload)  # again: nothing more
    assert all(stored(tree[p]) for p in ("after.txt", "staged.txt", "a.txt"))
    # A storage namespace outside the bucket is not this service's to clean
    get_repository = s.client.get_repository

    async def elsewhere(**kwargs):
        return {
            **await get_repository(**kwargs),
            "storage_namespace": "local://elsewhere/x",
        }

    monkeypatch.setattr(
        type(s.client), "get_repository", lambda self, **kw: elsewhere(**kw)
    )
    await s.cleanup.forget_squashed_history(payload)


async def test_writes_caught_by_a_squash_after_their_first_check_wait_or_retry(
    s, owner_client, monkeypatch
):
    """A write that passed the first check before a squash took the
    repository waits at LakeFS's door, and answers 409 if it waits too long."""
    repo = await _new(s, owner_client, "squash-door")
    first = await repo.commit(_file("a.txt", "a"))
    await repo.commit(_file("a.txt", "b"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "x", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    response = await owner_client.post(
        f"/api/models/{repo.id}/tag", json={"tag": "t", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    monkeypatch.setattr(
        s.lock, "ensure_free", lambda repo: None
    )  # as if checked just before
    monkeypatch.setattr(s.lock, "WAIT_SECONDS", 0.2)
    row = _row(s, repo.id)
    token = s.lock.acquire(row.id, "squash")
    base = f"/api/models/{repo.id}"
    refused = [
        await owner_client.post(
            f"{base}/branch", json={"branch": "y", "revision": "main"}
        ),
        await owner_client.delete(f"{base}/branch/x"),
        await owner_client.post(f"{base}/tag", json={"tag": "u", "revision": "main"}),
        await owner_client.delete(f"{base}/tag/t"),
        await owner_client.post(f"{base}/branch/main/revert", json={"ref": first}),
        await owner_client.post(
            f"{base}/branch/main/reset", json={"ref": first, "force": True}
        ),
        await owner_client.post(f"{base}/merge/x/into/main", json={}),
    ]
    assert [r.status_code for r in refused] == [409] * len(refused)
    assert all(r.json()["detail"]["operation"] == "squash" for r in refused)
    s.lock.release(row.id, token)
    assert await _refs(s, repo) == (["main", "x"], ["t"])  # nothing happened


async def test_a_branch_squashed_alone_cannot_be_merged_back(s, owner_client):
    repo = await _new(s, owner_client, "squash-unrelated")
    await repo.commit(_file("a.txt", "a"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(_file("dev.txt", "d"), branch="dev")
    response = await owner_client.post(
        f"/api/models/{repo.id}/super-squash/main", json={}
    )
    assert response.status_code == 200, response.text
    response = await owner_client.post(
        f"/api/models/{repo.id}/merge/dev/into/main", json={}
    )
    assert (
        response.status_code == 409
        and "share no history" in response.json()["detail"]["error"]
    )
