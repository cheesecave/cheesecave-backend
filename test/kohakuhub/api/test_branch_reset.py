"""Resetting a branch: a new commit whose tree equals the target (#99).

Everything runs against the real database, LakeFS and bucket.
"""

import hashlib

import httpx
import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _linear, _live, lfs


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
    # LFS files link the global object again: same identity, nothing re-uploaded
    tree = await _tree(m, repo, head)
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


async def test_big_resets_page_through_every_change(m, owner_client, monkeypatch):
    repo = await Repo(m, owner_client, "reset-big").create()
    before = await repo.commit(_file("keep.txt", "k"), lfs("keep.bin", b"keep"))
    ops = [_file(f"many/f{i:03d}.txt", f"text {i}") for i in range(160)]
    ops += [lfs(f"many/l{i:03d}.bin", f"lfs {i}".encode()) for i in range(90)]
    await repo.commit(*ops, _delete("keep.txt"))
    # Small pages and batches, so every loop turns more than once
    monkeypatch.setattr(m.avail, "PAGE", 100)
    monkeypatch.setattr(m.reset, "DELETE_BATCH", 100)
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
    """The result equals the target however the branch moved meanwhile; the
    concurrent commits stay in the history."""
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-race")
    merge = m.rest.LakeFSRestClient.merge_into_branch
    # a.bin: the same path, whose target version wins the merge; late.txt:
    # another path, merged alongside, so the reset runs once more
    commits = iter([(lfs("a.bin", b"a v9"), _file("late.txt", "late"))])
    concurrent = []

    async def racing_merge(self, *args, **kwargs):
        ops = next(commits, None)
        if ops:
            concurrent.append(await repo.commit(*ops))
        return await merge(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", racing_merge)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert response.json()["commit_id"] == head
    assert await _differs(m, repo, head, c1) == []
    log = await _pages(
        lambda after: m.client.log_commits(
            repository=repo.lakefs_repo, ref="main", after=after, amount=1000
        )
    )
    assert set(concurrent) <= {c["id"] for c in log}
    # Round 1 merged despite a.bin changing on both sides (the target's
    # version wins); late.txt then took round 2
    resets = [c for c in log if (c.get("metadata") or {}).get("reset_to") == c1]
    assert len(resets) == 2
    assert _files(m, repo)["late.txt"][2] is True
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}
    assert _scratch_branches(await m.client.list_branches(repo.lakefs_repo)) == []


async def test_a_branch_that_keeps_moving_gives_up(m, owner_client, monkeypatch):
    """Conflicts LakeFS cannot settle every time: nothing merged, 409."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-moving")

    async def conflict(self, *args, **kwargs):
        request = httpx.Request("POST", "http://lakefs")
        response = httpx.Response(409, request=request, text="conflict")
        raise httpx.HTTPStatusError("conflict", request=request, response=response)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", conflict)
    head = await repo.head()
    response = await _reset(repo, c1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "kept changing" in detail["error"] and detail["commits"] == []
    assert await repo.head() == head
    assert _scratch_branches(await m.client.list_branches(repo.lakefs_repo)) == []


async def test_giving_up_after_a_merge_still_records_it(m, owner_client, monkeypatch):
    """A merge that went in before the branch kept changing is on the branch:
    its bookkeeping is done, and the answer names it."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-moving-after")
    merge = m.rest.LakeFSRestClient.merge_into_branch
    counter = iter(range(100))

    async def racing_merge(self, *args, **kwargs):
        # A new file before every merge: merged alongside, so never the target
        await repo.commit(_file(f"late{next(counter)}.txt", "late"))
        return await merge(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", racing_merge)
    response = await _reset(repo, c1)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert len(detail["commits"]) == 3 and "reset commit" in detail["error"]
    made = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=detail["commits"][0])
    assert made["metadata"]["reset_to"] == c1
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}
    assert _files(m, repo)["b.bin"][2] is True
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == made["id"]) is not None


async def test_an_upload_in_flight_is_waited_for(m, owner_client, monkeypatch):
    """LakeFS refuses to merge into a branch with uncommitted changes (a commit
    being uploaded); the reset tries again from the head that commit makes."""
    repo, (initial, c1, c2, *_rest) = await _linear(m, owner_client, "reset-dirty")
    merge = m.rest.LakeFSRestClient.merge_into_branch
    calls = []

    async def dirty_merge(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await m.client.upload_object(
                repository=repo.lakefs_repo, branch="main", path="upload.txt", content=b"u"
            )
        elif len(calls) == 2:
            await m.client.commit(repository=repo.lakefs_repo, branch="main", message="upload")
        return await merge(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", dirty_merge)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert await _differs(m, repo, await repo.head(), c1) == []

    # Other refusals are errors, not retried
    async def refused(self, *args, **kwargs):
        request = httpx.Request("POST", "http://lakefs")
        raise httpx.HTTPStatusError(
            "bad", request=request, response=httpx.Response(400, request=request, text="bad")
        )

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", refused)
    head = await repo.head()
    response = await _reset(repo, c2)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "LakeFS refused the merge: bad"
    assert await repo.head() == head

    async def broken(self, *args, **kwargs):
        request = httpx.Request("POST", "http://lakefs")
        raise httpx.HTTPStatusError(
            "down", request=request, response=httpx.Response(503, request=request, text="down")
        )

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", broken)
    queued = []
    monkeypatch.setattr(
        _live("kohakuhub.api.branches"), "enqueue_lfs_reconciliation", lambda: queued.append(1)
    )
    assert (await _reset(repo, c2)).status_code == 500
    assert await repo.head() == head
    assert queued == [1]  # a merge may have landed: the reconciliation records it


async def test_a_branch_that_stays_dirty_is_named(m, owner_client, monkeypatch):
    """Uncommitted changes that do not go away (a commit that failed while
    uploading): the merge is tried again without rebuilding, then refused."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-stays-dirty")
    await m.client.upload_object(
        repository=repo.lakefs_repo, branch="main", path="stuck.txt", content=b"s"
    )
    builds = []
    build = m.reset._build

    async def counting_build(*args, **kwargs):
        builds.append(1)
        return await build(*args, **kwargs)

    monkeypatch.setattr(m.reset, "_build", counting_build)
    head = await repo.head()
    response = await _reset(repo, c1)
    assert response.status_code == 409
    assert "uncommitted changes" in response.json()["detail"]["error"]
    assert builds == [1]  # one tree, merged three times
    assert await repo.head() == head


async def test_a_failure_while_building_leaves_the_branch_alone(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-broken")

    async def broken_copy(self, *args, **kwargs):
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "copy_object", broken_copy)
    response = await _reset(repo, c1)
    assert response.status_code == 500
    assert "object store unavailable" in response.json()["detail"]["error"]
    assert await repo.head() == c5
    assert _scratch_branches(await m.client.list_branches(repo.lakefs_repo)) == []

    # Failing to drop the scratch branch does not fail a reset that worked
    monkeypatch.undo()
    monkeypatch.setattr(m.cfg.app, "repository_reset_enabled", True)
    monkeypatch.setattr(m.records, "RETRY_DELAY", 0)

    async def stuck(self, *args, **kwargs):
        raise RuntimeError("cannot delete")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "delete_branch", stuck)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert await _differs(m, repo, await repo.head(), c1) == []


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


async def test_files_stored_outside_the_lfs_prefix_are_copied(m, owner_client):
    """Big files a repository stores as regular objects (older resets did)
    are copied inside the object store, and keep their recorded identity."""
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


async def test_a_branch_that_came_to_equal_the_target_needs_nothing_more(
    m, owner_client, monkeypatch
):
    """A merge refused because someone else just made the branch equal the
    target: the reset is done, with no commit of its own."""
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "reset-meanwhile")

    merge = m.rest.LakeFSRestClient.merge_into_branch
    calls = []

    async def overtaken(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await repo.commit(lfs("a.bin", b"a v1"), _file("r.txt", "r1"), _delete("b.bin"))
        return await merge(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", overtaken)
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert response.json()["commit_id"] == head and head != c5
    assert await _differs(m, repo, head, c1) == []

    # Newer LakeFS refuses a merge with nothing to add: done as well
    repo2, ids = await _linear(m, owner_client, "reset-meanwhile-2")

    async def nothing_to_add(self, *args, **kwargs):
        await repo2.commit(lfs("a.bin", b"a v1"), _file("r.txt", "r1"), _delete("b.bin"))
        request = httpx.Request("POST", "http://lakefs")
        response = httpx.Response(400, request=request, text='{"message":"no changes"}')
        raise httpx.HTTPStatusError("no changes", request=request, response=response)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", nothing_to_add)
    commits = m.db.Commit.select().count()
    response = await _reset(repo2, ids[1])
    assert response.status_code == 200, response.text
    assert response.json()["commit_id"] == await repo2.head()
    assert m.db.Commit.select().count() == commits + 1  # the other commit only


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


async def test_a_failure_after_a_merge_still_records_it(m, owner_client, monkeypatch):
    """The first merge is on the branch; the second round fails. The reset
    commit is recorded, and the answer names it (#117 review)."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-late-failure")
    merge = m.rest.LakeFSRestClient.merge_into_branch
    first = []

    async def racing_merge(self, *args, **kwargs):
        if not first:
            first.append(1)
            await repo.commit(_file("late.txt", "late"))  # merged alongside
        return await merge(self, *args, **kwargs)

    async def broken_delete(self, *args, **kwargs):
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", racing_merge)
    monkeypatch.setattr(m.rest.LakeFSRestClient, "delete_objects", broken_delete)
    # c1 has b.bin deleted: take the head's b.bin out first, so round 1 deletes nothing
    await repo.commit(_delete("b.bin"))
    response = await _reset(repo, c1)
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "object store unavailable" in detail["error"] and len(detail["commits"]) == 1
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == detail["commits"][0]) is not None


async def test_a_claim_refused_after_a_merge_still_records_it(m, owner_client, monkeypatch):
    repo = await Repo(m, owner_client, "reset-late-claim").create()
    target = await repo.commit(lfs("a.bin", b"a v1"), _file("x.txt", "x1"))
    await repo.commit(_file("x.txt", "x2"))
    merge = m.rest.LakeFSRestClient.merge_into_branch
    first = []

    async def racing_merge(self, *args, **kwargs):
        if not first:
            first.append(1)
            await repo.commit(lfs("a.bin", b"a racing"))  # another path than round 1's
        return await merge(self, *args, **kwargs)

    def collected(sha256, exists_in_storage):
        raise m.gc.LfsObjectUnavailable(sha256)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", racing_merge)
    monkeypatch.setattr(m.records, "claim_for_commit", collected)  # only round 2 claims
    response = await _reset(repo, target)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["missing_files"] == ["a.bin"] and len(detail["commits"]) == 1
    assert _files(m, repo)["x.txt"] == (blob_sha1("x1"), False, False)
    # a.bin is the racing commit's, as the branch holds it
    assert _head_refs(m, repo) == {("a.bin", sha(b"a racing"))}


async def test_giving_up_records_what_the_branch_holds(m, owner_client, monkeypatch):
    """A concurrent commit changes a path the reset already merged, then the
    reset gives up: the records follow the branch, not the target (#117 review)."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-gives-up-holds")
    merge = m.rest.LakeFSRestClient.merge_into_branch
    counter = iter(range(100))

    async def racing_merge(self, *args, **kwargs):
        # A new file before every merge keeps the reset going; right after it,
        # a.bin, which the reset changed, changes again
        n = next(counter)
        await repo.commit(_file(f"late{n}.txt", "late"))
        merged = await merge(self, *args, **kwargs)
        await repo.commit(lfs("a.bin", f"a racing {n}".encode()))
        return merged

    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", racing_merge)
    response = await _reset(repo, c1)
    assert response.status_code == 409
    commits = response.json()["detail"]["commits"]
    assert commits
    tree = await _tree(m, repo, await repo.head())
    held = m.gc.lfs_oid(tree["a.bin"]["physical_address"])
    assert held != sha(b"a v1")
    assert _head_refs(m, repo) == {("a.bin", held)}
    assert _files(m, repo)["a.bin"][0] == held
    # The first round's version is attributed to the commit that merged it
    H = m.db.LFSObjectHistory
    first = H.get_or_none((H.commit_id == commits[0]) & (H.path_in_repo == "a.bin"))
    assert first.sha256 == sha(b"a v1") and first.file is not None
    after = H.get(H.sha256 == sha(b"a racing 0"))
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
    """A reconciliation listing branches while a reset runs skips its scratch
    branch, and nothing recorded under its name survives it."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-scratch")
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
    assert (await _reset(repo, c1)).status_code == 200
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
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-drop-refs")
    queued = []

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(m.reset, "drop_head_refs", broken)
    monkeypatch.setattr(m.reset, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text  # the merge is kept and recorded
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v1"))}
    assert queued == [1]


async def test_the_commits_are_recorded_even_if_reading_the_branch_fails(
    m, owner_client, monkeypatch
):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "reset-books-early")
    merge = m.rest.LakeFSRestClient.merge_into_branch
    get_branch = m.rest.LakeFSRestClient.get_branch
    merged = []

    async def tracking_merge(self, *args, **kwargs):
        result = await merge(self, *args, **kwargs)
        merged.append(result["reference"])
        return result

    async def failing_after_merge(self, *args, **kwargs):
        if merged:
            raise RuntimeError("LakeFS unavailable")
        return await get_branch(self, *args, **kwargs)

    queued = []
    monkeypatch.setattr(m.rest.LakeFSRestClient, "merge_into_branch", tracking_merge)
    monkeypatch.setattr(m.rest.LakeFSRestClient, "get_branch", failing_after_merge)
    monkeypatch.setattr(m.records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _reset(repo, c1)
    assert response.status_code == 200, response.text
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == merged[0]) is not None
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
