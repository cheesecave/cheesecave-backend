"""Resetting a branch: a new commit whose tree equals the target (#99).

Everything runs against the real database, LakeFS and bucket.
"""

import hashlib

import httpx
import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _linear, _live, lfs
from test.kohakuhub.support.db import history_operations_need_postgres


pytestmark = history_operations_need_postgres


@pytest.fixture
def m(prepared_backend_test_state, monkeypatch):
    cfg = _live("kohakuhub.config").cfg
    monkeypatch.setattr(cfg.app, "repository_reset_enabled", True)
    ns = type("M", (), {})()
    ns.cfg = cfg
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.avail = _live("kohakuhub.api.commit.availability")
    ns.reset = _live("kohakuhub.api.commit.reset")
    ns.records = _live("kohakuhub.api.commit.records")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.rest._singleton_client = None
    ns.client = ns.lakefs.get_lakefs_client()
    monkeypatch.setattr(ns.records, "RETRY_DELAY", 0)
    yield ns
    ns.db.LfsObjectTombstone.delete().execute()
    ns.rest._singleton_client = None


def collect(m, sha256):
    """As garbage collection leaves an object: tombstoned and deleted."""
    m.db.LfsObjectTombstone.create(sha256=sha256, state=m.gc.DELETED)
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(sha256))


def sha(content):
    return hashlib.sha256(content).hexdigest()


def blob_sha1(text):
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


async def _reset(repo, target, branch="main", force=True, client=None, **extra):
    return await (client or repo.client).post(
        f"/api/models/{repo.id}/branch/{branch}/reset",
        json={"ref": target, "force": force, **extra},
    )


async def _pages(fetch):
    out, after = [], ""
    while True:
        page = await fetch(after)
        out += page["results"]
        if not page["pagination"]["has_more"]:
            return out
        after = page["pagination"]["next_offset"]


async def _tree(m, repo, ref):
    objects = await _pages(
        lambda after: m.client.list_objects(
            repository=repo.lakefs_repo, ref=ref, after=after, amount=1000
        )
    )
    return {o["path"]: o for o in objects}


async def _differs(m, repo, left, right):
    return await _pages(
        lambda after: m.client.diff_refs(
            repository=repo.lakefs_repo,
            left_ref=left,
            right_ref=right,
            after=after,
            amount=1000,
            diff_type="two_dot",
        )
    )


def _row(m, repo):
    return m.db.Repository.get(m.db.Repository.full_id == repo.id)


def _files(m, repo):
    F = m.db.File
    return {
        f.path_in_repo: (f.sha256, f.lfs, f.is_deleted)
        for f in F.select().where(F.repository == _row(m, repo))
    }


def _head_refs(m, repo, branch="main"):
    R = m.db.LfsHeadRef
    return {
        (r.path_in_repo, r.sha256)
        for r in R.select().where((R.repository == _row(m, repo)) & (R.branch == branch))
    }


def _scratch_branches(branches):
    return [b["id"] for b in branches["results"] if b["id"].startswith("kh-reset-")]


def _status_error(status, text):
    request = httpx.Request("POST", "http://lakefs")
    response = httpx.Response(status, request=request, text=text)
    return httpx.HTTPStatusError(text, request=request, response=response)


def _on_reset_commit(m, monkeypatch, before=None, after=None):
    """Run ``before``/``after`` around the reset's commit (the one carrying a
    ``source_metarange``); other commits go through untouched."""
    commit = m.rest.LakeFSRestClient.commit
    calls = []

    async def wrapped(self, repository, branch, *args, **kwargs):
        if not kwargs.get("source_metarange"):
            return await commit(self, repository, branch, *args, **kwargs)
        calls.append(kwargs["source_metarange"])
        if before:
            await before(len(calls))
        made = await commit(self, repository, branch, *args, **kwargs)
        if after:
            await after(made)
        return made

    monkeypatch.setattr(m.rest.LakeFSRestClient, "commit", wrapped)
    return calls


async def test_reset_restores_the_target_exactly_and_moves_no_content(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-basic")
    response = await _reset(repo, c1, message="Back to the first weights")
    assert response.status_code == 200, response.text
    body = response.json()
    head = await repo.head()
    assert body == {
        "success": True,
        "message": f"Successfully reset branch 'main' to commit {c1[:8]} (new commit created)",
        "commit_id": head,
    }
    # One linear commit on top of the old head, whose tree equals the target
    assert await _differs(m, repo, head, c1) == []
    commit = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    assert commit["parents"] == [c5]
    assert commit["message"] == "Back to the first weights"
    assert commit["metadata"]["reset_to"] == c1
    # Nothing copied: every entry is the target's own object, regular files too
    tree, target_tree = await _tree(m, repo, head), await _tree(m, repo, c1)
    addresses = {path: entry["physical_address"] for path, entry in tree.items()}
    assert addresses == {path: entry["physical_address"] for path, entry in target_tree.items()}
    assert m.gc.lfs_oid(tree["a.bin"]["physical_address"]) == sha(b"a v1")
    assert "b.bin" not in tree
    # The database follows, for the paths the reset changed
    files = _files(m, repo)
    assert files["a.bin"] == (sha(b"a v1"), True, False)
    assert files["r.txt"] == (blob_sha1("r1"), False, False)
    assert files["b.bin"][2] is True
    H = m.db.LFSObjectHistory
    assert H.get_or_none((H.commit_id == head) & (H.path_in_repo == "a.bin")).sha256 == sha(b"a v1")
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == head) is not None
    # What the head no longer links is left to garbage collection
    C = m.db.LfsGcCandidate
    candidates = {c.sha256 for c in C.select()}
    assert {sha(b"b v1"), sha(b"a v3")} <= candidates
    assert _scratch_branches(await m.client.list_branches(repo.lakefs_repo)) == []

    # The default message
    response = await _reset(repo, c3)
    head = await repo.head()
    commit = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    assert commit["message"] == f"Reset to commit {c3[:8]}"
    assert await _differs(m, repo, head, c3) == []


async def test_the_target_metarange_is_committed_and_no_object_is_touched(
    m, owner_client, monkeypatch
):
    """The reset is one LakeFS commit of the target's own metarange: no object
    is copied, linked or uploaded, so it works whatever the storage (#133)."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-metarange")

    async def forbidden(*args, **kwargs):
        raise AssertionError("a reset touches no object and needs no scratch branch")

    for name in ("link_physical_address", "upload_object", "create_branch", "delete_objects"):
        monkeypatch.setattr(m.rest.LakeFSRestClient, name, forbidden)
    calls = _on_reset_commit(m, monkeypatch)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    target = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=c1)
    assert calls == [target["meta_range_id"]]
    made = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=await repo.head())
    assert made["meta_range_id"] == target["meta_range_id"]


async def test_big_resets_page_through_every_change(m, owner_client, monkeypatch):
    repo = await Repo(m, owner_client, "reset-big").create()
    before = await repo.commit(_file("keep.txt", "k"), lfs("keep.bin", b"keep"))
    ops = [_file(f"many/f{i:03d}.txt", f"text {i}") for i in range(160)]
    ops += [lfs(f"many/l{i:03d}.bin", f"lfs {i}".encode()) for i in range(90)]
    await repo.commit(*ops, _delete("keep.txt"))
    # Small pages, so every listing turns more than once
    monkeypatch.setattr(m.avail, "PAGE", 100)
    response = await _reset(repo, before)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert await _differs(m, repo, head, before) == []
    files = _files(m, repo)
    assert files["keep.txt"] == (blob_sha1("k"), False, False)
    assert all(files[f"many/f{i:03d}.txt"][2] for i in range(160))
    assert _head_refs(m, repo) == {("keep.bin", sha(b"keep"))}

    # And back: 90 LFS objects to restore, claimed in slices
    await repo.commit(_file("again.txt", "a"))
    monkeypatch.setattr(m.records, "CLAIM_YIELD", 10)
    target = (
        await _pages(
            lambda after: m.client.log_commits(
                repository=repo.lakefs_repo, ref="main", after=after, amount=1000
            )
        )
    )[2][
        "id"
    ]  # the commit adding many/: before the reset and again.txt
    response = await _reset(repo, target)
    assert response.status_code == 200, response.text
    assert await _differs(m, repo, await repo.head(), target) == []
    assert len(_head_refs(m, repo)) == 91


async def test_reset_to_the_initial_commit_empties_the_branch(m, owner_client, monkeypatch):
    """The initial commit has no metarange (an empty tree): a scratch branch
    with every file deleted provides one, and is dropped afterwards."""
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-initial")
    first = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=initial)
    assert not first["meta_range_id"]  # the case under test
    monkeypatch.setattr(m.reset, "DELETE_BATCH", 1)  # one batch per file
    response = await _reset(repo, initial)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert await _tree(m, repo, head) == {}
    commit = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    assert commit["parents"] == [c5] and commit["metadata"]["reset_to"] == initial
    assert all(deleted for _, _, deleted in _files(m, repo).values())
    assert _head_refs(m, repo) == set()
    assert _scratch_branches(await m.client.list_branches(repo.lakefs_repo)) == []

    # A failure building the empty tree leaves the branch alone, and no scratch
    await repo.commit(_file("again.txt", "a"))
    head = await repo.head()

    async def broken_delete(self, *args, **kwargs):
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "delete_objects", broken_delete)
    response = await _reset(repo, initial)
    assert response.status_code == 500
    assert "object store unavailable" in response.json()["detail"]["error"]
    assert await repo.head() == head
    assert _scratch_branches(await m.client.list_branches(repo.lakefs_repo)) == []

    # Failing to drop the scratch branch does not fail a reset that worked
    monkeypatch.undo()
    monkeypatch.setattr(m.cfg.app, "repository_reset_enabled", True)
    monkeypatch.setattr(m.records, "RETRY_DELAY", 0)

    async def stuck(self, *args, **kwargs):
        raise RuntimeError("cannot delete")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "delete_branch", stuck)
    response = await _reset(repo, initial)
    assert response.status_code == 200, response.text
    assert await _tree(m, repo, await repo.head()) == {}


async def test_a_file_no_longer_stored_refuses_even_with_force(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-missing")
    collect(m, sha(b"a v1"))
    response = await _reset(repo, c1, force=True)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["missing_files"] == ["a.bin"] and detail["recoverable"] is False
    assert "no longer stored" in detail["error"]
    assert await repo.head() == c5

    # Gone from the bucket without a tombstone counts too
    m.db.LfsObjectTombstone.delete().execute()
    repo.put(b"a v1")
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(sha(b"a v2")))
    response = await _reset(repo, c2)
    assert response.status_code == 400
    assert response.json()["detail"]["missing_files"] == ["a.bin"]
    assert await repo.head() == c5
    # Many missing files: the message names five
    many = [lfs(f"w/{i}.bin", f"weights {i}".encode()) for i in range(7)]
    with_many = await repo.commit(*many)
    await repo.commit(*(_delete(f"w/{i}.bin") for i in range(7)))
    for i in range(7):
        collect(m, sha(f"weights {i}".encode()))
    detail = (await _reset(repo, with_many)).json()["detail"]
    assert len(detail["missing_files"]) == 7 and detail["error"].endswith("and 2 more")
    # Only what the target needs and the head lacks counts: b.bin is deleted
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(sha(b"b v1")))
    assert (await _reset(repo, c1)).status_code == 200


async def test_guardrails_keep_their_behaviour(m, owner_client, visitor_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-guards")
    response = await _reset(repo, c3, force=False)
    assert response.status_code == 400 and "force=true" in response.json()["detail"]["error"]
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": c5}
    )
    assert response.status_code == 200, response.text
    assert (await _reset(repo, c3, branch="dev", force=False)).status_code == 200
    assert await repo.head() == c5  # main untouched

    for force in (False, True):
        response = await _reset(repo, c3, branch="dev", force=force)
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "Branch is already at the target state"
    assert (await _reset(repo, "f" * 64)).status_code == 404
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "kh-reset-mine", "revision": c5}
    )
    assert response.status_code == 400 and "reserved" in response.headers["x-error-message"]
    assert (await _reset(repo, c3, client=visitor_client)).status_code in (403, 404)


async def test_a_concurrent_commit_is_reset_too(m, owner_client, monkeypatch):
    """A commit landing between reading the head and committing becomes the
    reset commit's parent: the result still equals the target, the concurrent
    commit stays in the history, and every path it touched is recorded."""
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-race")
    concurrent = []

    async def racing(n):
        # a.bin: a path the reset changes anyway; late.txt and new.bin: paths
        # it did not plan to touch, which the target lacks
        if n == 1:
            ops = (lfs("a.bin", b"a v9"), _file("late.txt", "late"), lfs("new.bin", b"new"))
            concurrent.append(await repo.commit(*ops))

    _on_reset_commit(m, monkeypatch, before=racing)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert response.json()["commit_id"] == head
    assert await _differs(m, repo, head, c1) == []
    made = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    assert made["parents"] == concurrent and made["metadata"]["reset_to"] == c1
    files = _files(m, repo)
    assert files["late.txt"][2] is True and files["new.bin"][2] is True
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}  # new.bin's reference is gone
    H = m.db.LFSObjectHistory
    assert H.get((H.commit_id == head) & (H.path_in_repo == "a.bin")).sha256 == sha(b"a v1")


async def test_a_branch_that_came_to_equal_the_target_is_done(m, owner_client, monkeypatch):
    """Someone else makes the branch equal the target while the reset runs: the
    reset's commit changes nothing more, and is recorded like any other."""
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-meanwhile")
    concurrent = []

    async def overtaken(n):
        concurrent.append(
            await repo.commit(lfs("a.bin", b"a v1"), _file("r.txt", "r1"), _delete("b.bin"))
        )

    _on_reset_commit(m, monkeypatch, before=overtaken)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert response.json()["commit_id"] == head and head not in concurrent
    assert await _differs(m, repo, head, c1) == []
    made = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    assert made["parents"] == concurrent
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == head) is not None
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}


async def test_an_upload_in_flight_is_waited_for(m, owner_client, monkeypatch):
    """LakeFS refuses a ``source_metarange`` commit while the branch has
    uncommitted changes (a commit being uploaded); the reset tries again, and
    undoes that commit too."""
    repo, (initial, c1, c2, *_rest) = await _linear(m, owner_client, "reset-dirty")
    commit = m.rest.LakeFSRestClient.commit

    async def uploading(n):
        if n == 1:  # staged: LakeFS refuses the reset's commit
            await m.client.upload_object(
                repository=repo.lakefs_repo, branch="main", path="upload.txt", content=b"u"
            )
        elif n == 2:  # its commit lands before the next try
            await m.client.commit(repository=repo.lakefs_repo, branch="main", message="upload")

    calls = _on_reset_commit(m, monkeypatch, before=uploading)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    head = await repo.head()
    assert await _differs(m, repo, head, c1) == []
    assert "upload.txt" not in await _tree(m, repo, head)

    # Other refusals are errors, not retried
    async def refused(self, repository, branch, *args, **kwargs):
        if kwargs.get("source_metarange"):
            raise _status_error(400, "bad")
        return await commit(self, repository, branch, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "commit", refused)
    head = await repo.head()
    response = await _reset(repo, c2)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "LakeFS refused the commit: bad"
    assert await repo.head() == head

    async def broken(self, repository, branch, *args, **kwargs):
        if kwargs.get("source_metarange"):
            raise _status_error(503, "down")
        return await commit(self, repository, branch, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "commit", broken)
    queued = []
    records = _live("kohakuhub.api.commit.records")
    monkeypatch.setattr(records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    recounts = []
    monkeypatch.setattr(records.usage, "enqueue_repository_recount", recounts.append)
    assert (await _reset(repo, c2)).status_code == 500
    assert await repo.head() == head
    # A commit may have landed: the reconciliation records it, main is recounted
    assert queued == [1] and recounts == [_row(m, repo).id]


async def test_a_branch_that_stays_dirty_is_named(m, owner_client, monkeypatch):
    """Uncommitted changes that do not go away (a commit that failed while
    uploading): the commit is tried a few times, then refused."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-stays-dirty")
    await m.client.upload_object(
        repository=repo.lakefs_repo, branch="main", path="stuck.txt", content=b"s"
    )
    calls = _on_reset_commit(m, monkeypatch)
    head = await repo.head()
    response = await _reset(repo, c1)
    assert response.status_code == 409
    assert "uncommitted changes" in response.json()["detail"]["error"]
    assert len(calls) == m.records.DIRTY_WAITS
    assert await repo.head() == head


async def test_a_failed_commit_leaves_the_branch_alone(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-broken")
    commit = m.rest.LakeFSRestClient.commit

    async def broken(self, repository, branch, *args, **kwargs):
        if kwargs.get("source_metarange"):
            raise RuntimeError("LakeFS unavailable")
        return await commit(self, repository, branch, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "commit", broken)
    response = await _reset(repo, c1)
    assert response.status_code == 500
    assert "LakeFS unavailable" in response.json()["detail"]["error"]
    assert await repo.head() == c5


async def test_collection_racing_the_reset_is_caught_when_claiming(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-claim")

    def collected(sha256, exists_in_storage):
        raise m.gc.LfsObjectUnavailable(sha256)

    monkeypatch.setattr(m.records, "claim_for_commit", collected)
    response = await _reset(repo, c1)
    assert response.status_code == 400
    assert response.json()["detail"]["missing_files"] == ["a.bin"]

    # Revived from a tombstone, but deleted from the bucket after it was checked
    def revived(sha256, exists_in_storage):
        m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(sha256))
        return True

    monkeypatch.setattr(m.records, "claim_for_commit", revived)
    response = await _reset(repo, c1)
    assert response.status_code == 400
    assert await repo.head() == c5
    # ... so its tombstone is back
    assert m.gc.tombstone_state(sha(b"a v1")) == m.gc.DELETED


async def test_an_object_uploaded_again_after_collection_is_revived(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-revived")
    m.db.LfsObjectTombstone.create(sha256=sha(b"a v1"), state=m.gc.DELETED)  # still stored
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert m.gc.tombstone_state(sha(b"a v1")) is None


async def test_files_stored_outside_the_lfs_prefix_keep_their_object(m, owner_client):
    """Big files a repository stores as regular objects (older resets did)
    come back as the very object they were, with their recorded identity."""
    repo = await Repo(m, owner_client, "reset-legacy").create()
    row = _row(m, repo)
    row.lfs_threshold_bytes = 1_000_000
    row.save()
    await m.client.upload_object(
        repository=repo.lakefs_repo, branch="main", path="legacy.bin", content=b"L" * 1_000_001
    )
    await m.client.commit(repository=repo.lakefs_repo, branch="main", message="legacy")
    before = await repo.head()
    await repo.commit(_delete("legacy.bin"))
    response = await _reset(repo, before)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert await _differs(m, repo, head, before) == []
    entry = (await _tree(m, repo, head))["legacy.bin"]
    assert entry["physical_address"] == (await _tree(m, repo, before))["legacy.bin"]["physical_address"]
    assert m.gc.lfs_oid(entry["physical_address"]) is None
    assert _files(m, repo)["legacy.bin"] == (entry["checksum"], True, False)


async def test_bookkeeping_failures_do_not_fail_a_reset_that_happened(m, owner_client, monkeypatch):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-books")
    queued = []

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(m.records, "record_evicted_versions", broken)
    monkeypatch.setattr(m.records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert await _differs(m, repo, await repo.head(), c1) == []
    assert queued == [1]  # the reconciliation repairs what was not recorded


async def test_versions_pushed_out_of_the_keep_window_go_to_collection(
    m, owner_client, monkeypatch
):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-window")
    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    monkeypatch.setattr(m.cfg.app, "lfs_keep_versions", 1)
    queued = []
    monkeypatch.setattr(m.records, "enqueue_lfs_collection", lambda: queued.append(1))
    assert (await _reset(repo, c1)).status_code == 200
    # a.bin's newest version is v1 again: v3 and v2 fall out of a window of one
    assert queued == [1]
    candidates = {c.sha256 for c in m.db.LfsGcCandidate.select()}
    assert sha(b"a v2") in candidates


async def test_a_failure_after_a_concurrent_commit_still_records_the_reset(
    m, owner_client, monkeypatch
):
    """The reset commit is on the branch when reading what the concurrent
    commit changed fails: it is recorded, and the answer names it."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-late-failure")
    changed_paths = m.avail.changed_paths
    committed = []

    async def racing(n):
        await repo.commit(_file("late.txt", "late"))

    async def mark(commit):
        committed.append(commit["id"])

    async def failing_once_committed(*args, **kwargs):
        if committed:
            raise RuntimeError("LakeFS unavailable")
        return await changed_paths(*args, **kwargs)

    _on_reset_commit(m, monkeypatch, before=racing, after=mark)
    monkeypatch.setattr(m.avail, "changed_paths", failing_once_committed)
    queued = []
    monkeypatch.setattr(m.reset, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _reset(repo, c1)
    assert response.status_code == 500
    # What late.txt's commit changed is unknown: the reconciliation records it
    assert queued == [1]
    detail = response.json()["detail"]
    assert "LakeFS unavailable" in detail["error"]
    assert detail["commits"] == committed == [await repo.head()]
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == committed[0]) is not None
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}


async def test_a_claim_refused_after_a_concurrent_commit_still_records_it(
    m, owner_client, monkeypatch
):
    """The concurrent commit changed a path whose target version must now be
    claimed too; collection got there first. The reset commit is on the branch:
    it is recorded as the branch holds it, and the answer names the file."""
    repo = await Repo(m, owner_client, "reset-late-claim").create()
    target = await repo.commit(lfs("a.bin", b"a v1"), _file("x.txt", "x1"))
    await repo.commit(_file("x.txt", "x2"))  # the reset itself claims nothing

    async def racing(n):
        await repo.commit(lfs("a.bin", b"a racing"))

    def collected(sha256, exists_in_storage):
        raise m.gc.LfsObjectUnavailable(sha256)

    _on_reset_commit(m, monkeypatch, before=racing)
    monkeypatch.setattr(m.records, "claim_for_commit", collected)
    response = await _reset(repo, target)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["missing_files"] == ["a.bin"]
    assert detail["commits"] == [await repo.head()]
    # Not "Cannot reset": the reset commit is in
    assert detail["error"].startswith(f"Reset committed as {detail['commits'][0][:8]} on top of")
    assert detail["error"].endswith("are no longer stored (garbage collected or missing): a.bin")
    assert _files(m, repo)["x.txt"] == (blob_sha1("x1"), False, False)
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}


async def test_records_follow_the_branch_not_the_target(m, owner_client, monkeypatch):
    """A commit landing right after the reset's changes a path the reset
    changed: the records follow what the branch holds (#117 review)."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-holds")
    made = []

    async def racing(commit):
        made.append(commit["id"])
        await repo.commit(lfs("a.bin", b"a after the reset"))

    _on_reset_commit(m, monkeypatch, after=racing)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert response.json()["commit_id"] == made[0]
    assert _head_refs(m, repo) == {("a.bin", sha(b"a after the reset"))}
    assert _files(m, repo)["a.bin"][0] == sha(b"a after the reset")
    # The reset's version is attributed to the reset commit
    H = m.db.LFSObjectHistory
    first = H.get_or_none((H.commit_id == made[0]) & (H.path_in_repo == "a.bin"))
    assert first.sha256 == sha(b"a v1") and first.file is not None
    after = H.get(H.sha256 == sha(b"a after the reset"))
    assert first.created_at <= after.created_at


async def test_an_unreadable_regular_file_keeps_its_row(m, owner_client, monkeypatch):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-unreadable")
    before = _files(m, repo)["r.txt"]

    async def unreadable(self, *args, **kwargs):
        raise RuntimeError("read failed")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "get_object", unreadable)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert _files(m, repo)["r.txt"] == before
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == head) is not None


async def test_the_scratch_branch_protects_nothing(m, owner_client, monkeypatch):
    """A reconciliation listing branches while a reset to the initial commit
    builds its empty tree skips the scratch branch, and nothing recorded under
    its name survives it."""
    repo, (initial, *_rest) = await _linear(m, owner_client, "reset-scratch")
    cleanup = _live("kohakuhub.storage_cleanup")
    seen = []
    commit = m.rest.LakeFSRestClient.commit

    async def listing_commit(self, repository, branch, *args, **kwargs):
        if branch.startswith(cleanup.SCRATCH_BRANCH_PREFIX):
            heads, *_ = await cleanup.branch_head_references(repository)
            seen.append({b for b, _, _ in heads})
            m.gc.add_head_refs(_row(m, repo), [(branch, "x.bin", "0" * 64)])
        return await commit(self, repository, branch, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "commit", listing_commit)
    assert (await _reset(repo, initial)).status_code == 200
    assert seen == [{"main"}]
    R = m.db.LfsHeadRef
    assert not R.select().where(R.branch.startswith(cleanup.SCRATCH_BRANCH_PREFIX)).exists()


async def test_storage_usage_follows_and_its_failure_is_harmless(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, *_rest) = await _linear(m, owner_client, "reset-usage")
    counted = []
    count = m.records.count_main_move

    async def spy(client, lakefs_repo, repo_row, commit):
        counted.append(repo_row.full_id)
        await count(client, lakefs_repo, repo_row, commit)

    monkeypatch.setattr(m.records, "count_main_move", spy)
    assert (await _reset(repo, c1)).status_code == 200
    assert counted == [repo.id]

    async def broken(*args, **kwargs):
        raise RuntimeError("LakeFS hiccup")

    readable = m.records.availability

    class Unreadable:  # only counting the move cannot read the diff
        def __getattr__(self, name):
            return broken if name == "changes" else getattr(readable, name)

    monkeypatch.setattr(m.records, "count_main_move", count)
    monkeypatch.setattr(m.records, "availability", Unreadable())
    assert (await _reset(repo, c2)).status_code == 200
    queued = m.db.BackgroundTask.select().where(m.db.BackgroundTask.kind == "usage.recount_repository")
    assert queued.exists()  # counted by a recount instead


async def test_a_failure_dropping_the_scratch_references_is_harmless(m, owner_client, monkeypatch):
    repo, (initial, *_rest) = await _linear(m, owner_client, "reset-drop-refs")
    queued = []

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(m.reset, "drop_head_refs", broken)
    monkeypatch.setattr(m.reset, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _reset(repo, initial)
    assert response.status_code == 200, response.text  # the commit is kept and recorded
    assert _head_refs(m, repo) == set()
    assert queued == [1]


async def test_the_commits_are_recorded_even_if_reading_the_branch_fails(
    m, owner_client, monkeypatch
):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-books-early")
    get_branch = m.rest.LakeFSRestClient.get_branch
    made = []

    async def track(commit):
        made.append(commit["id"])

    async def failing_after_commit(self, *args, **kwargs):
        if made:
            raise RuntimeError("LakeFS unavailable")
        return await get_branch(self, *args, **kwargs)

    queued = []
    _on_reset_commit(m, monkeypatch, after=track)
    monkeypatch.setattr(m.rest.LakeFSRestClient, "get_branch", failing_after_commit)
    monkeypatch.setattr(m.records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == made[0]) is not None
    assert queued == [1]

    # And a commit row that cannot be written does not stop the rest
    monkeypatch.undo()
    monkeypatch.setattr(m.cfg.app, "repository_reset_enabled", True)
    monkeypatch.setattr(m.records, "RETRY_DELAY", 0)

    def no_commit_rows(**kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(m.records, "create_commit", no_commit_rows)
    ids = await _pages(
        lambda after: m.client.log_commits(
            repository=repo.lakefs_repo, ref="main", after=after, amount=1000
        )
    )
    response = await _reset(repo, ids[2]["id"])
    assert response.status_code == 200, response.text
