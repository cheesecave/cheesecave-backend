"""Commits to one branch run one at a time (#56), and a stale parent is refused (#57).

LakeFS commits whatever is staged on a branch, so two requests staging at once made one commit
carry the other's files: the second answered 500 or "no changes" while its files were in the
first's commit, its File rows were undone, and with the rows gone the LFS collector deleted
the objects a day later. Now a branch has one writer at a time; any other request is refused at
once with the same answer and leaves nothing behind.

Everything runs against the real database, LakeFS and bucket.
"""

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from kohakuhub import lakefs_rest_client
from test.kohakuhub.api.commit.test_availability import Repo, _file, _live, lfs
from test.kohakuhub.api.helpers import encode_ndjson


@pytest.fixture
def m(prepared_backend_test_state, monkeypatch):
    lakefs_rest_client._singleton_client = None
    ns = SimpleNamespace()
    ns.cfg = _live("kohakuhub.config").cfg
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.lock = _live("kohakuhub.api.repo.utils.branch_lock")
    # No waiting in the queue unless a test asks for it: a branch another request holds is refused at once
    monkeypatch.setattr(ns.lock, "QUEUE_SECONDS", 0)
    ns.cleanup = _live("kohakuhub.storage_cleanup")
    ns.ops = _live("kohakuhub.api.commit.routers.operations")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.client = lakefs_rest_client.LakeFSRestClient(
        endpoint=ns.cfg.lakefs.endpoint,
        access_key=ns.cfg.lakefs.access_key,
        secret_key=ns.cfg.lakefs.secret_key,
    )
    yield ns
    ns.db.LfsObjectTombstone.delete().execute()
    ns.db.BranchCommitLock.delete().execute()
    lakefs_rest_client._singleton_client = None


async def _new(m, client, name):
    return await Repo(m, client, name).create()


def _row(m, repo):
    return m.db.Repository.get(m.db.Repository.full_id == repo.id)


async def _post(m, repo, ops, *, branch="main", summary="change", parent=None, client=None):
    """POST a commit without asserting on the answer."""
    for op in ops:
        if op["key"] == "lfsFile" and "_content" in op["value"]:  # else: never uploaded
            repo.put(op["value"].pop("_content"))
    header = {"summary": summary}
    if parent is not None:
        header["parentCommit"] = parent
    return await (client or repo.client).post(
        f"/api/models/{repo.id}/commit/{branch}",
        content=encode_ndjson([{"key": "header", "value": header}, *ops]),
        headers={"Content-Type": "application/x-ndjson"},
    )


async def _tree(m, repo, ref="main"):
    page = await m.client.list_objects(repository=repo.lakefs_repo, ref=ref, amount=1000)
    return {o["path"] for o in page["results"]}


async def _staged(m, repo, branch="main"):
    """What LakeFS holds uncommitted on the branch: [] once everything is committed or undone."""
    async with httpx.AsyncClient(
        auth=(m.cfg.lakefs.access_key, m.cfg.lakefs.secret_key), timeout=60
    ) as http:
        response = await http.get(
            f"{m.cfg.lakefs.endpoint}/api/v1/repositories/{repo.lakefs_repo}/branches/{branch}/diff",
            params={"amount": 1000},
        )
    response.raise_for_status()
    return [(d["type"], d["path"]) for d in response.json()["results"]]


async def _commits(m, repo, ref="main"):
    return (await m.client.log_commits(repository=repo.lakefs_repo, ref=ref, amount=1000))["results"]


async def _added_by(m, repo, commit):
    """The paths a commit changed against its first parent."""
    parent = commit["parents"][0]
    diff = await m.client.diff_refs(
        repository=repo.lakefs_repo, left_ref=parent, right_ref=commit["id"], amount=1000
    )
    return {d["path"] for d in diff["results"]}


def _file_paths(m, repo, branch="main"):
    F = m.db.File
    return {
        f.path_in_repo
        for f in F.select().where(
            (F.repository == _row(m, repo)) & (F.branch == branch) & (F.is_deleted == False)  # noqa: E712
        )
    }


async def _collect_a_day_later(m):
    """Run the LFS collector as the worker would once the upload grace period is over."""
    m.db.LfsRecentObject.update(touched_at=m.db.utcnow() - timedelta(days=2)).execute()
    m.gc.expire_recent()
    m.gc.mark_references_reconciled()
    await m.cleanup.collect_lfs({}, _live("kohakuhub.task_testing").RecordingContext())


def _in_storage(m, oid):
    try:
        m.s3.head_object(Bucket=m.cfg.s3.bucket, Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}")
        return True
    except Exception:
        return False


def _is_busy(response):
    return (
        response.status_code == 429  # what the Hub answers when a commit waited too long in its queue
        and response.headers.get("retry-after") == "2"
        and response.json()["detail"]["error"] == "maximum time in concurrency queue reached"
        and response.headers.get("x-error-message") == response.json()["detail"]["error"]
    )


# --- the race -------------------------------------------------------------------------------


async def test_commits_racing_on_one_branch_never_share_files(m, owner_client):
    repo = await _new(m, owner_client, "serial-regular")
    initial = await repo.head()

    responses = await asyncio.gather(
        *(
            _post(m, repo, [_file(f"par/{i}.txt", f"content {i}")], summary=f"commit {i}")
            for i in range(10)
        )
    )

    statuses = [r.status_code for r in responses]
    assert set(statuses) <= {200, 429}, statuses  # never a 500
    winners = {i for i, r in enumerate(responses) if r.status_code == 200}
    assert winners
    assert all(_is_busy(r) for r in responses if r.status_code == 429)
    # what the branch holds is what the winners wrote: nothing of a loser, nothing staged
    assert await _tree(m, repo) == {f"par/{i}.txt" for i in winners}
    assert _file_paths(m, repo) == {f"par/{i}.txt" for i in winners}
    assert await _staged(m, repo) == []
    # and every commit carries exactly its own request's file
    by_oid = {responses[i].json()["commitOid"]: i for i in winners}
    commits = [c for c in await _commits(m, repo) if c["id"] != initial]
    assert {c["id"] for c in commits} == set(by_oid)
    for commit in commits:
        assert await _added_by(m, repo, commit) == {f"par/{by_oid[commit['id']]}.txt"}


async def test_every_request_lands_when_the_refused_try_again(m, owner_client):
    repo = await _new(m, owner_client, "serial-retry")
    initial = await repo.head()
    contents = {i: f"lfs content {i} ".encode() * 20 for i in range(4)}

    async def until_it_lands(i):
        # half of them are regular files, half LFS
        for _ in range(400):
            op = lfs(f"par/{i}.bin", contents[i]) if i < 4 else _file(f"par/{i}.txt", f"c{i}")
            response = await _post(m, repo, [op], summary=f"commit {i}")
            if response.status_code == 200:
                return response.json()["commitOid"]
            assert _is_busy(response), response.text
            await asyncio.sleep(0.02)
        raise AssertionError(f"request {i} never landed")

    oids = await asyncio.gather(*(until_it_lands(i) for i in range(8)))

    expected = {f"par/{i}.bin" for i in range(4)} | {f"par/{i}.txt" for i in range(4, 8)}
    assert await _tree(m, repo) == expected
    assert _file_paths(m, repo) == expected
    commits = [c for c in await _commits(m, repo) if c["id"] != initial]
    assert {c["id"] for c in commits} == set(oids)
    for oid, i in zip(oids, range(8)):
        path = f"par/{i}.bin" if i < 4 else f"par/{i}.txt"
        commit = next(c for c in commits if c["id"] == oid)
        assert await _added_by(m, repo, commit) == {path}

    # the objects of the LFS files are linked, so a day later the collector keeps them
    for i in range(4):
        oid = hashlib.sha256(contents[i]).hexdigest()
        assert m.gc.retention_reason(oid, now=m.db.utcnow() + timedelta(hours=25)) == "head"
    await _collect_a_day_later(m)
    for i in range(4):
        assert _in_storage(m, hashlib.sha256(contents[i]).hexdigest())


async def test_lfs_files_of_racing_commits_survive_the_collector(m, owner_client):
    """The data loss of #56: a request answered 200 whose File rows were undone, so its LFS
    object linked nothing and the collector deleted it after the grace period."""
    repo = await _new(m, owner_client, "serial-lfs")
    contents = {i: f"racing lfs {i} ".encode() * 30 for i in range(8)}

    responses = await asyncio.gather(
        *(_post(m, repo, [lfs(f"par/{i}.bin", contents[i])]) for i in range(8))
    )

    assert {r.status_code for r in responses} <= {200, 429}
    winners = [i for i, r in enumerate(responses) if r.status_code == 200]
    assert winners
    await _collect_a_day_later(m)
    for i in winners:
        assert _in_storage(m, hashlib.sha256(contents[i]).hexdigest()), f"par/{i}.bin was collected"
    assert await _tree(m, repo) == {f"par/{i}.bin" for i in winners}


async def test_commits_racing_on_one_branch_queue_up_and_all_land(m, owner_client, monkeypatch):
    """The Hub queues concurrent commits: every request lands, in its own commit, no retry needed."""
    monkeypatch.setattr(m.lock, "QUEUE_SECONDS", 60)
    repo = await _new(m, owner_client, "serial-queue")
    initial = await repo.head()
    contents = {i: f"queued lfs {i} ".encode() * 20 for i in range(4)}

    def op(i):
        return lfs(f"par/{i}.bin", contents[i]) if i < 4 else _file(f"par/{i}.txt", f"c{i}")

    responses = await asyncio.gather(
        *(_post(m, repo, [op(i)], summary=f"commit {i}") for i in range(12))
    )

    assert [r.status_code for r in responses] == [200] * 12, [r.text[:80] for r in responses if r.status_code != 200]
    expected = {f"par/{i}.bin" for i in range(4)} | {f"par/{i}.txt" for i in range(4, 12)}
    assert await _tree(m, repo) == expected and _file_paths(m, repo) == expected
    assert await _staged(m, repo) == []
    by_oid = {responses[i].json()["commitOid"]: i for i in range(12)}
    commits = [c for c in await _commits(m, repo) if c["id"] != initial]
    assert {c["id"] for c in commits} == set(by_oid)  # one commit each
    for commit in commits:
        i = by_oid[commit["id"]]
        path = f"par/{i}.bin" if i < 4 else f"par/{i}.txt"
        assert await _added_by(m, repo, commit) == {path}
    await _collect_a_day_later(m)
    for i in range(4):
        assert _in_storage(m, hashlib.sha256(contents[i]).hexdigest())


# --- the one that cannot get the branch ----------------------------------------------------


async def test_a_refused_commit_leaves_nothing_behind(m, owner_client):
    repo = await _new(m, owner_client, "serial-refused")
    head = await repo.head()
    content = b"refused lfs content " * 20
    token = m.lock.acquire(_row(m, repo).id, "main")
    assert token

    response = await _post(
        m, repo, [_file("a.txt", "a"), lfs("b.bin", content)], summary="refused"
    )

    assert _is_busy(response), response.text
    assert await _staged(m, repo) == []
    assert await _tree(m, repo) == set()
    assert _file_paths(m, repo) == set()
    refs = m.db.LfsHeadRef.select().where(m.db.LfsHeadRef.repository == _row(m, repo))
    assert refs.count() == 0
    assert await repo.head() == head
    # and the holder's lease was not touched by the refusal
    assert m.lock.is_held(_row(m, repo).id, "main", token)

    m.lock.release(_row(m, repo).id, "main", token)
    response = await _post(m, repo, [_file("a.txt", "a"), lfs("b.bin", content)])
    assert response.status_code == 200, response.text


async def test_the_lease_is_per_branch(m, owner_client):
    repo = await _new(m, owner_client, "serial-branches")
    created = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev"}
    )
    assert created.status_code == 200, created.text
    token = m.lock.acquire(_row(m, repo).id, "main")

    assert _is_busy(await _post(m, repo, [_file("a.txt", "a")], branch="main"))
    other = await _post(m, repo, [_file("b.txt", "b")], branch="dev")
    assert other.status_code == 200, other.text
    m.lock.release(_row(m, repo).id, "main", token)


async def test_the_lease_is_given_back_after_every_outcome(m, owner_client):
    repo = await _new(m, owner_client, "serial-release")
    row_id = _row(m, repo).id
    ok = await _post(m, repo, [_file("a.txt", "a")])
    assert ok.status_code == 200
    assert m.db.BranchCommitLock.select().count() == 0
    # nothing to commit
    again = await _post(m, repo, [_file("a.txt", "a")])
    assert again.status_code == 200
    assert m.db.BranchCommitLock.select().count() == 0
    # a commit that fails while staging: an LFS object that was never uploaded
    missing = {
        "key": "lfsFile",
        "value": {"path": "x.bin", "oid": "e" * 64, "size": 10, "algo": "sha256"},
    }
    failed = await _post(m, repo, [missing])
    assert failed.status_code >= 400
    assert m.db.BranchCommitLock.select().count() == 0
    assert await _staged(m, repo) == []
    assert m.lock.acquire(row_id, "main")


async def test_a_crashed_holder_does_not_block_the_branch(m, owner_client):
    repo = await _new(m, owner_client, "serial-crashed")
    row_id = _row(m, repo).id
    m.lock.acquire(row_id, "main")
    m.db.BranchCommitLock.update(until=m.db.utcnow() - timedelta(seconds=1)).execute()

    response = await _post(m, repo, [_file("a.txt", "a")])

    assert response.status_code == 200, response.text
    assert m.db.BranchCommitLock.select().count() == 0


async def test_a_commit_that_lost_its_lease_does_not_reach_lakefs(m, owner_client, monkeypatch):
    """The lease is checked right before LakeFS commits: if it was lost meanwhile (expired and
    taken), the commit is undone instead of committing over the new holder's staging.
    The stage step is wrapped only to take the lease away at that moment."""
    repo = await _new(m, owner_client, "serial-lost")
    head = await repo.head()
    row_id = _row(m, repo).id
    real = m.ops._stage_operations

    async def stage_then_lose_the_lease(*args, **kwargs):
        result = await real(*args, **kwargs)
        m.db.BranchCommitLock.delete().execute()
        m.lock.acquire(row_id, "main")  # somebody else holds it now
        return result

    monkeypatch.setattr(m.ops, "_stage_operations", stage_then_lose_the_lease)

    response = await _post(m, repo, [_file("a.txt", "a")])

    assert response.status_code == 503, response.text  # nothing was committed: retried with backoff
    assert "lost" in response.json()["detail"]["error"]
    assert await repo.head() == head
    assert await _staged(m, repo) == []
    assert _file_paths(m, repo) == set()


# --- the parent a commit expects (#57) -----------------------------------------------------


async def test_a_stale_parent_is_refused_and_leaves_nothing_behind(m, owner_client):
    repo = await _new(m, owner_client, "serial-parent")
    parent = await repo.head()
    moved = await repo.commit(_file("first.txt", "first"))

    response = await _post(m, repo, [_file("second.txt", "second")], parent=parent)

    assert response.status_code == 412, response.text
    assert response.json()["detail"]["error"] == (
        "A commit has happened since. Please refresh and try again."
    )
    assert response.headers["x-error-message"] == response.json()["detail"]["error"]
    assert await repo.head() == moved
    assert await _tree(m, repo) == {"first.txt"}
    assert await _staged(m, repo) == []
    assert _file_paths(m, repo) == {"first.txt"}
    assert m.db.BranchCommitLock.select().count() == 0


async def test_the_current_parent_commits_whole_or_abbreviated(m, owner_client):
    repo = await _new(m, owner_client, "serial-parent-ok")
    head = await repo.head()

    first = await _post(m, repo, [_file("a.txt", "a")], parent=head)
    assert first.status_code == 200, first.text
    second = await _post(
        m, repo, [_file("b.txt", "b")], parent=first.json()["commitOid"][:7]
    )
    assert second.status_code == 200, second.text
    assert await _tree(m, repo) == {"a.txt", "b.txt"}


async def test_two_commits_on_the_same_parent_keep_one(m, owner_client, monkeypatch):
    monkeypatch.setattr(m.lock, "QUEUE_SECONDS", 30)  # they queue, then the parent is stale for all but one
    repo = await _new(m, owner_client, "serial-same-parent")
    parent = await repo.head()

    responses = await asyncio.gather(
        *(
            _post(m, repo, [_file(f"par/{i}.txt", str(i))], parent=parent)
            for i in range(5)
        )
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 412, 412, 412, 412], codes
    winner = next(i for i, r in enumerate(responses) if r.status_code == 200)
    assert await _tree(m, repo) == {f"par/{winner}.txt"}


async def test_a_parent_on_a_missing_branch_is_a_missing_revision(m, owner_client):
    repo = await _new(m, owner_client, "serial-parent-missing")

    response = await _post(
        m, repo, [_file("a.txt", "a")], branch="nowhere", parent="a" * 40
    )

    assert response.status_code == 404, response.text
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert m.db.BranchCommitLock.select().count() == 0


async def test_a_parent_that_is_not_a_commit_id_is_a_bad_request(m, owner_client):
    repo = await _new(m, owner_client, "serial-parent-type")

    response = await _post(m, repo, [_file("a.txt", "a")], parent=12345)

    assert response.status_code == 400, response.text
    assert "parentCommit" in response.json()["detail"]["error"]
    assert response.headers["x-error-message"] == response.json()["detail"]["error"]
    assert await _staged(m, repo) == []
    assert m.db.BranchCommitLock.select().count() == 0


async def test_a_parent_check_that_lakefs_cannot_answer_fails_the_commit_cleanly(
    m, owner_client, monkeypatch
):
    """LakeFS (an external service) failing while the branch head is read: the commit answers
    the same 500 as any other LakeFS failure, stages nothing and gives the lease back."""
    repo = await _new(m, owner_client, "serial-parent-down")

    async def unavailable(self, repository, branch):
        request = httpx.Request("GET", "http://lakefs/branches")
        raise httpx.HTTPStatusError(
            "LakeFS API error 503", request=request, response=httpx.Response(503, request=request)
        )

    live_client = _live("kohakuhub.lakefs_rest_client").LakeFSRestClient  # what the route uses
    monkeypatch.setattr(live_client, "get_branch", unavailable)

    response = await _post(m, repo, [_file("a.txt", "a")], parent="a" * 40)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["error"].startswith("Commit failed:")
    monkeypatch.undo()
    assert await _staged(m, repo) == []
    assert m.db.BranchCommitLock.select().count() == 0


# --- content that was already there ----------------------------------------------------------


async def test_committing_content_again_restores_a_missing_regular_row(m, owner_client):
    """Rows lost before this fix: the same content again is "no changes" for LakeFS, but the
    rows it describes are true and stay (they used to be undone, so the damage was permanent)."""
    repo = await _new(m, owner_client, "heal-regular")
    first = await repo.commit(_file("a.txt", "same"))
    F = m.db.File
    F.delete().where(F.repository == _row(m, repo)).execute()
    assert _file_paths(m, repo) == set()

    response = await _post(m, repo, [_file("a.txt", "same")])

    assert response.status_code == 200, response.text
    assert response.json()["commitOid"] == first  # nothing new was committed
    assert _file_paths(m, repo) == {"a.txt"}
    assert await _staged(m, repo) == []


async def test_committing_content_again_restores_the_links_of_an_lfs_file(m, owner_client):
    repo = await _new(m, owner_client, "heal-lfs")
    content = b"healed lfs content " * 30
    oid = hashlib.sha256(content).hexdigest()
    first = await repo.commit(lfs("w.bin", content))
    row = _row(m, repo)
    m.db.File.delete().where(m.db.File.repository == row).execute()
    m.db.LfsHeadRef.delete().where(m.db.LfsHeadRef.repository == row).execute()
    later = m.db.utcnow() + timedelta(hours=25)
    m.db.LFSObjectHistory.delete().where(m.db.LFSObjectHistory.repository == row).execute()
    assert m.gc.retention_reason(oid, now=later) is None  # the state the old bug left

    response = await _post(m, repo, [lfs("w.bin", content)])

    assert response.status_code == 200, response.text
    assert response.json()["commitOid"] == first
    assert _file_paths(m, repo) == {"w.bin"}
    assert m.gc.retention_reason(oid, now=later) == "head"
    await _collect_a_day_later(m)
    assert _in_storage(m, oid)


async def test_unchanged_lfs_content_is_linked_again(m, owner_client):
    """A file whose row survives (a read puts a missing row back) but whose links were lost: the
    same content committed again links it, with nothing else changing."""
    repo = await _new(m, owner_client, "heal-linked")
    content = b"links lost, row kept " * 30
    oid = hashlib.sha256(content).hexdigest()
    first = await repo.commit(lfs("w.bin", content))
    row = _row(m, repo)
    m.db.LfsHeadRef.delete().where(m.db.LfsHeadRef.repository == row).execute()
    m.db.LFSObjectHistory.delete().where(m.db.LFSObjectHistory.repository == row).execute()
    m.db.File.update(is_deleted=False).where(m.db.File.repository == row).execute()  # the row stays
    later = m.db.utcnow() + timedelta(hours=25)
    assert m.gc.retention_reason(oid, now=later) == "file"  # only the row keeps it

    response = await _post(m, repo, [lfs("w.bin", content)])

    assert response.status_code == 200 and response.json()["commitOid"] == first
    assert m.gc.retention_reason(oid, now=later) == "head"


async def test_unchanged_lfs_content_uploaded_again_revives_what_was_collected(m, owner_client):
    """The object was collected, the row came back with a read, and the file is uploaded again: the
    commit has nothing to change, but the object is claimed (its tombstone goes), so the
    reconciliation stops counting the file as lost."""
    repo = await _new(m, owner_client, "heal-revive")
    content = b"collected, row healed, uploaded again " * 20
    oid = hashlib.sha256(content).hexdigest()
    first = await repo.commit(lfs("w.bin", content))
    row = _row(m, repo)
    m.db.LfsHeadRef.delete().where(m.db.LfsHeadRef.repository == row).execute()
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}")
    m.db.LfsObjectTombstone.create(sha256=oid, state=m.gc.DELETED)
    assert not _in_storage(m, oid)

    # the client uploads the bytes again, then commits the same content
    response = await _post(m, repo, [lfs("w.bin", content)])

    assert response.status_code == 200, response.text
    assert response.json()["commitOid"] == first
    assert _in_storage(m, oid)
    assert m.gc.tombstone_state(oid) is None
    assert m.gc.retention_reason(oid, now=m.db.utcnow() + timedelta(hours=25)) == "head"


async def test_unchanged_lfs_content_that_is_gone_must_be_uploaded_again(m, owner_client):
    """A tombstoned object that is not in storage: the commit says so (409) instead of reporting
    "no changes" for a file nobody can download."""
    repo = await _new(m, owner_client, "heal-gone-unchanged")
    content = b"collected and not uploaded again " * 20
    oid = hashlib.sha256(content).hexdigest()
    await repo.commit(lfs("w.bin", content))
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}")
    m.db.LfsObjectTombstone.create(sha256=oid, state=m.gc.DELETED)
    op = lfs("w.bin", content)
    op["value"].pop("_content")  # nothing is uploaded

    response = await _post(m, repo, [op])

    assert response.status_code == 409, response.text
    assert "Upload it again" in response.json()["detail"]["error"]
    assert m.db.BranchCommitLock.select().count() == 0


async def test_a_commit_that_changes_nothing_still_answers_the_head(m, owner_client):
    repo = await _new(m, owner_client, "heal-unchanged")
    first = await repo.commit(_file("a.txt", "same"))

    response = await _post(m, repo, [_file("a.txt", "same")])

    assert response.status_code == 200
    assert response.json()["commitOid"] == first
    assert m.db.BranchCommitLock.select().count() == 0


# --- what the old race left behind ---------------------------------------------------------


async def _damage(m, repo):
    """The state the old race left: no rows, no links, no history for the files of a repository,
    and a reconciliation due (what the upgrade does)."""
    row = _row(m, repo)
    m.db.File.delete().where(m.db.File.repository == row).execute()
    m.db.LfsHeadRef.delete().where(m.db.LfsHeadRef.repository == row).execute()
    m.db.LFSObjectHistory.delete().where(m.db.LFSObjectHistory.repository == row).execute()
    m.db.LfsGcState.delete().execute()


def _reconcile_context():
    return _live("kohakuhub.task_testing").RecordingContext(
        kind=_live("kohakuhub.storage_cleanup").RECONCILE_LFS_KIND
    )


async def test_the_reconciliation_saves_files_the_old_race_left_unlinked(m, owner_client):
    repo = await _new(m, owner_client, "heal-reconcile")
    content = b"damaged by the old race " * 30
    oid = hashlib.sha256(content).hexdigest()
    await repo.commit(lfs("w.bin", content))
    await _damage(m, repo)
    later = m.db.utcnow() + timedelta(hours=25)
    assert m.gc.retention_reason(oid, now=later) is None  # the collector would take it
    m.db.LfsRecentObject.update(touched_at=m.db.utcnow() - timedelta(days=2)).execute()

    # the hourly expiry of uploads waits: a reconciliation is due, and queued by it
    await m.cleanup.expire_recent_lfs({})
    assert m.db.LfsRecentObject.select().count() > 0
    assert m.db.BackgroundTask.select().where(
        m.db.BackgroundTask.kind == m.cleanup.RECONCILE_LFS_KIND
    ).exists()

    ctx = _reconcile_context()
    await m.cleanup.reconcile_lfs_references({}, ctx)

    stats = ctx.checkpoint_state["stats"]
    assert stats["head_refs_added"] >= 1 and stats["files_fixed"] >= 1
    assert "linked_objects_collected" not in stats
    assert _file_paths(m, repo) == {"w.bin"}
    assert m.gc.retention_reason(oid, now=later) == "head"
    # and the collector, a day later, leaves it alone
    await m.cleanup.expire_recent_lfs({})
    await m.cleanup.collect_lfs({}, _live("kohakuhub.task_testing").RecordingContext())
    assert _in_storage(m, oid)


async def test_the_reconciliation_reports_files_whose_content_is_already_gone(m, owner_client):
    """Not everything can be saved: an object collected before this fix is gone, and the file
    that links it is listed but cannot be downloaded. The reconciliation counts them, so an
    operator knows which files to upload again."""
    repo = await _new(m, owner_client, "heal-gone")
    content = b"collected before the fix " * 30
    oid = hashlib.sha256(content).hexdigest()
    await repo.commit(lfs("w.bin", content), _file("a.txt", "a"))
    await _damage(m, repo)
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}")
    m.db.LfsObjectTombstone.create(sha256=oid, state=m.gc.DELETED)

    ctx = _reconcile_context()
    await m.cleanup.reconcile_lfs_references({}, ctx)

    assert ctx.checkpoint_state["stats"]["linked_objects_collected"] == 1
    # No row and no link for it: a row saying the file is there would make the next upload of
    # the same content "unchanged", and the client would never send the bytes again
    assert _file_paths(m, repo) == set()
    assert m.db.LfsHeadRef.select().where(m.db.LfsHeadRef.sha256 == oid).count() == 0
    preupload = await owner_client.post(
        f"/api/models/{repo.id}/preupload/main",
        json={"files": [{"path": "w.bin", "size": len(content), "sha256": oid}]},
    )
    assert preupload.status_code == 200, preupload.text
    assert preupload.json()["files"][0]["shouldIgnore"] is False
    # uploading the same content again is a commit of what the branch holds: the object is
    # back, and so are its row and links
    again = await _post(m, repo, [lfs("w.bin", content)])
    assert again.status_code == 200, again.text
    assert _in_storage(m, oid)
    assert m.gc.tombstone_state(oid) is None
    assert _file_paths(m, repo) == {"w.bin"}
    assert m.gc.retention_reason(oid, now=m.db.utcnow() + timedelta(hours=25)) == "head"


async def test_the_reconciliation_does_not_restore_the_row_of_a_lost_file(m, owner_client):
    """A row marked deleted whose path is on the branch head is normally made active again. For a
    file whose object is already gone that would say the file is there, and its upload would be
    "unchanged" from then on."""
    repo = await _new(m, owner_client, "heal-gone-deleted")
    content = b"collected, row deleted, still on the head " * 20
    oid = hashlib.sha256(content).hexdigest()
    await repo.commit(lfs("w.bin", content))
    await _damage(m, repo)
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}")
    m.db.LfsObjectTombstone.create(sha256=oid, state=m.gc.DELETED)
    m.db.File.create(
        repository=_row(m, repo), branch="main", path_in_repo="w.bin", sha256=oid,
        size=len(content), lfs=True, is_deleted=True, owner=_row(m, repo).owner_id,
    )

    ctx = _reconcile_context()
    await m.cleanup.reconcile_lfs_references({}, ctx)

    assert ctx.checkpoint_state["stats"]["linked_objects_collected"] == 1
    assert _file_paths(m, repo) == set()  # still deleted: the file cannot be downloaded
    # a re-upload restores it, as it always did for a deleted file
    again = await _post(m, repo, [lfs("w.bin", content)])
    assert again.status_code == 200, again.text
    assert _file_paths(m, repo) == {"w.bin"} and _in_storage(m, oid)


async def test_a_branch_name_that_cannot_exist_is_a_missing_revision(m, owner_client):
    repo = await _new(m, owner_client, "serial-long-branch")

    response = await _post(m, repo, [_file("a.txt", "a")], branch="b" * 300)

    assert response.status_code == 404, response.text
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert m.db.BranchCommitLock.select().count() == 0
