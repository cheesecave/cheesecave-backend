"""Reverting a commit, and recording what merges change (#99).

Everything runs against the real database, LakeFS and bucket.
"""

import httpx
import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _linear, _live, lfs
from test.kohakuhub.api.test_branch_reset import (
    _differs,
    _files,
    _head_refs,
    _tree,
    blob_sha1,
    collect,
    sha,
)
from test.kohakuhub.support.db import history_operations_need_postgres


@pytest.fixture
def m(prepared_backend_test_state, monkeypatch):
    cfg = _live("kohakuhub.config").cfg
    monkeypatch.setattr(cfg.app, "repository_revert_enabled", True)
    ns = type("M", (), {})()
    ns.cfg = cfg
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.avail = _live("kohakuhub.api.commit.availability")
    ns.records = _live("kohakuhub.api.commit.records")
    ns.revert = _live("kohakuhub.api.commit.revert")
    ns.branches = _live("kohakuhub.api.branches")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.rest._singleton_client = None
    ns.client = ns.lakefs.get_lakefs_client()
    monkeypatch.setattr(ns.records, "RETRY_DELAY", 0)
    yield ns
    ns.db.LfsObjectTombstone.delete().execute()
    ns.rest._singleton_client = None


async def _revert(repo, commit, branch="main", client=None, **extra):
    return await (client or repo.client).post(
        f"/api/models/{repo.id}/branch/{branch}/revert", json={"ref": commit, **extra}
    )


def _refused(status, text):
    request = httpx.Request("POST", "http://lakefs")
    response = httpx.Response(status, request=request, text=text)
    return httpx.HTTPStatusError(text, request=request, response=response)


@history_operations_need_postgres
async def test_a_revert_undoes_the_commit_and_is_recorded(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-basic")
    response = await _revert(repo, c3, message="Drop b.bin")
    assert response.status_code == 200, response.text
    head = await repo.head()
    assert response.json() == {
        "success": True,
        "message": f"Successfully reverted commit {c3[:8]} on branch 'main'",
        "new_commit_id": head,
    }
    commit = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=head)
    assert commit["parents"] == [c5] and commit["message"] == "Drop b.bin"
    assert commit["metadata"]["revert_of"] == c3
    assert "b.bin" not in await _tree(m, repo, head)
    assert _files(m, repo)["b.bin"][2] is True
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v3"))}
    row = m.db.Commit.get(m.db.Commit.commit_id == head)
    assert row.description == f"Reverted {c3}"
    assert sha(b"b v1") in {c.sha256 for c in m.db.LfsGcCandidate.select()}

    # A regular file, then an LFS version restored: linked again, recorded
    assert (await _revert(repo, c4)).status_code == 200
    assert _files(m, repo)["r.txt"] == (blob_sha1("r1"), False, False)
    response = await _revert(repo, c5)
    assert response.status_code == 200, response.text
    new = response.json()["new_commit_id"]
    assert m.gc.lfs_oid((await _tree(m, repo, new))["a.bin"]["physical_address"]) == sha(b"a v2")
    assert _head_refs(m, repo) == {("a.bin", sha(b"a v2"))}
    H = m.db.LFSObjectHistory
    assert H.get((H.commit_id == new) & (H.path_in_repo == "a.bin")).sha256 == sha(b"a v2")
    # The default message, and force is accepted and ignored
    response = await _revert(repo, new, force=True)
    assert response.status_code == 200, response.text
    commit = await m.client.get_commit(
        repository=repo.lakefs_repo, commit_id=response.json()["new_commit_id"]
    )
    assert commit["message"] == f"Revert commit {new[:8]}"


@history_operations_need_postgres
async def test_a_conflict_names_its_files(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-conflict")
    response = await _revert(repo, c2)  # a.bin changed again in c5
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["conflicts"] == ["a.bin"] and "a.bin" in detail["error"]
    assert await repo.head() == c5

    # A tombstoned version only a conflicting path would need: the conflict
    # is what refuses the revert, not the version
    m.db.LfsObjectTombstone.create(sha256=sha(b"a v1"), state=m.gc.DELETED)
    response = await _revert(repo, c2)
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["a.bin"]


@history_operations_need_postgres
async def test_a_revived_version_to_restore_alongside_a_conflict(m, owner_client):
    """The claim revives a tombstoned version uploaded again on a path the
    revert would undo; the other path's conflict then refuses it."""
    repo = await Repo(m, owner_client, "revert-revived-conflict").create()
    await repo.commit(lfs("x.bin", b"x old"), lfs("y.bin", b"y old"))
    both = await repo.commit(lfs("x.bin", b"x new"), lfs("y.bin", b"y new"))
    await repo.commit(lfs("y.bin", b"y later"))  # y.bin: a conflict now
    m.db.LfsObjectTombstone.create(sha256=sha(b"x old"), state=m.gc.DELETED)  # still stored
    response = await _revert(repo, both)
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == ["y.bin"]
    assert m.gc.tombstone_state(sha(b"x old")) is None  # revived by the claim


@history_operations_need_postgres
async def test_a_version_no_longer_stored_refuses_it(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-missing")
    collect(m, sha(b"a v2"))
    for force in (False, True):  # force changes nothing
        response = await _revert(repo, c5, force=force)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["missing_files"] == ["a.bin"] and "no longer stored" in detail["error"]
    assert await repo.head() == c5


@history_operations_need_postgres
async def test_an_object_uploaded_again_after_collection_is_revived(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-revived")
    m.db.LfsObjectTombstone.create(sha256=sha(b"a v2"), state=m.gc.DELETED)  # still stored
    assert (await _revert(repo, c5)).status_code == 200
    assert m.gc.tombstone_state(sha(b"a v2")) is None


@history_operations_need_postgres
async def test_nothing_to_revert_and_allow_empty(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-nothing")
    assert (await _revert(repo, c3)).status_code == 200
    response = await _revert(repo, c3)
    assert response.status_code == 400
    assert response.json()["detail"]["error"].startswith("Nothing to revert")
    head = await repo.head()
    response = await _revert(repo, c3, allow_empty=True)
    assert response.status_code == 200, response.text
    empty = response.json()["new_commit_id"]
    assert await _differs(m, repo, head, empty) == []


@history_operations_need_postgres
async def test_guardrails(m, owner_client, visitor_client):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "revert-guards")
    response = await _revert(repo, initial)
    assert response.status_code == 400 and "first commit" in response.json()["detail"]["error"]
    response = await _revert(repo, c1, parent_number=2)
    assert response.status_code == 400 and "parent_number" in response.json()["detail"]["error"]
    assert (await _revert(repo, "f" * 64)).status_code == 404
    response = await _revert(repo, c1, branch="missing")
    assert response.status_code == 404 and "Branch not found" in response.text
    assert (await _revert(repo, c1, client=visitor_client)).status_code in (403, 404)


@history_operations_need_postgres
async def test_a_merge_commit_reverts_against_the_chosen_parent(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-merge")
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": c5}
    )
    assert response.status_code == 200, response.text
    await repo.commit(lfs("x.bin", b"x v1"), branch="dev")
    await repo.commit(_file("main.txt", "main side"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/merge/dev/into/main", json={"message": "Merge dev"}
    )
    assert response.status_code == 200, response.text
    merge_commit = await repo.head()
    # Against the first parent (main's side): undo what dev brought
    response = await _revert(repo, merge_commit, parent_number=1)
    assert response.status_code == 200, response.text
    assert "x.bin" not in await _tree(m, repo, response.json()["new_commit_id"])
    # Against the second parent (dev's side): undo what main did since
    response = await _revert(repo, merge_commit, parent_number=2)
    assert response.status_code == 200, response.text
    tree = await _tree(m, repo, response.json()["new_commit_id"])
    assert "main.txt" not in tree
    assert (await _revert(repo, merge_commit, parent_number=3)).status_code == 400


@history_operations_need_postgres
async def test_a_big_revert_records_every_path(m, owner_client, monkeypatch):
    repo = await Repo(m, owner_client, "revert-big").create()
    await repo.commit(_file("keep.txt", "k"))
    upload = await repo.commit(*(lfs(f"up/w{i:03d}.bin", f"up {i}".encode()) for i in range(150)))
    monkeypatch.setattr(m.avail, "PAGE", 100)  # every listing turns more than once
    response = await _revert(repo, upload)
    assert response.status_code == 200, response.text
    files = _files(m, repo)
    assert all(files[f"up/w{i:03d}.bin"][2] for i in range(150))
    assert _head_refs(m, repo) == set()
    # And back: 150 versions restored, each claimed and recorded
    removal = response.json()["new_commit_id"]
    response = await _revert(repo, removal)
    assert response.status_code == 200, response.text
    new = response.json()["new_commit_id"]
    assert len(_head_refs(m, repo)) == 150
    H = m.db.LFSObjectHistory
    assert H.select().where(H.commit_id == new).count() == 150


@history_operations_need_postgres
async def test_the_new_commit_is_found_by_its_marker(m, owner_client, monkeypatch):
    """A commit landing right after the revert must not be taken for it."""
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-marker")
    revert_branch = m.rest.LakeFSRestClient.revert_branch
    after = []

    async def followed(self, *args, **kwargs):
        await revert_branch(self, *args, **kwargs)
        after.append(await repo.commit(_file("late.txt", "late")))

    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", followed)
    monkeypatch.setattr(m.revert, "FIND_PAGE", 1)  # search page by page
    response = await _revert(repo, c3)
    assert response.status_code == 200, response.text
    new = response.json()["new_commit_id"]
    assert new != after[0] and await repo.head() == after[0]
    commit = await m.client.get_commit(repository=repo.lakefs_repo, commit_id=new)
    assert commit["metadata"]["revert_of"] == c3
    assert m.db.Commit.get(m.db.Commit.commit_id == new).description == f"Reverted {c3}"

    # Not found: an error, and the reconciliation records what the branch links
    async def unmarked(self, *args, **kwargs):
        kwargs["metadata"] = {**kwargs["metadata"], "kh_operation": "someone else's"}
        await revert_branch(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", unmarked)
    queued = []
    monkeypatch.setattr(m.records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _revert(repo, c4)  # the whole log searched
    assert response.status_code == 500 and queued == [1]
    assert "may have been applied" in response.json()["detail"]["error"]
    monkeypatch.setattr(m.revert, "FIND_DEPTH", 2)  # or only its newest commits
    response = await _revert(repo, c5)
    assert response.status_code == 500 and queued == [1, 1]


@history_operations_need_postgres
async def test_the_branch_changing_after_the_check(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-changed")
    revert_branch = m.rest.LakeFSRestClient.revert_branch
    change = {}

    async def raced(self, *args, **kwargs):
        await repo.commit(*change["ops"])
        return await revert_branch(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", raced)
    # a.bin changed again: now a conflict, named
    change["ops"] = (lfs("a.bin", b"a v4"),)
    response = await _revert(repo, c5)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "changed since the check" in detail["error"] and detail["conflicts"] == ["a.bin"]
    # b.bin deleted meanwhile: nothing left to revert
    change["ops"] = (_delete("b.bin"),)
    response = await _revert(repo, c3)
    assert response.status_code == 400
    assert response.json()["detail"]["error"].startswith("Nothing to revert")


@history_operations_need_postgres
async def test_an_upload_in_flight_is_waited_for(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-dirty")
    revert_branch = m.rest.LakeFSRestClient.revert_branch
    calls = []

    async def dirty(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            await m.client.upload_object(
                repository=repo.lakefs_repo, branch="main", path="upload.txt", content=b"u"
            )
        elif len(calls) == 2:
            await m.client.commit(repository=repo.lakefs_repo, branch="main", message="upload")
        return await revert_branch(self, *args, **kwargs)

    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", dirty)
    response = await _revert(repo, c3)
    assert response.status_code == 200, response.text
    assert len(calls) == 2

    # Uncommitted changes that stay: refused after the tries
    await m.client.upload_object(
        repository=repo.lakefs_repo, branch="main", path="stuck.txt", content=b"s"
    )
    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", revert_branch)
    response = await _revert(repo, c4)
    assert response.status_code == 409
    assert "uncommitted changes" in response.json()["detail"]["error"]


@history_operations_need_postgres
async def test_other_refusals_keep_their_status(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, *_rest) = await _linear(m, owner_client, "revert-refused")

    async def forbidden(self, *args, **kwargs):
        raise _refused(403, "protected branch")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", forbidden)
    response = await _revert(repo, c3)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "LakeFS refused the revert: protected branch"

    async def down(self, *args, **kwargs):
        raise _refused(503, "down")

    queued = []
    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", down)
    monkeypatch.setattr(m.records, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    assert (await _revert(repo, c3)).status_code == 500
    assert queued == [1]

    # A conflict LakeFS sees but the check does not (it compares checksums,
    # LakeFS object identities): said as such
    async def conflict(self, *args, **kwargs):
        raise _refused(409, "conflict found")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "revert_branch", conflict)
    response = await _revert(repo, c3)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["conflicts"] == [] and "LakeFS reported a conflict" in detail["error"]

    # Reading the branch failing otherwise than "not found" is an error
    async def branch_down(self, *args, **kwargs):
        raise _refused(503, "down")

    monkeypatch.setattr(m.rest.LakeFSRestClient, "get_branch", branch_down)
    assert (await _revert(repo, c3)).status_code == 500


@history_operations_need_postgres
async def test_collection_racing_the_claim_refuses_it(m, owner_client, monkeypatch):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "revert-claim")

    def collected(sha256, exists_in_storage):
        raise m.gc.LfsObjectUnavailable(sha256)

    monkeypatch.setattr(m.records, "claim_for_commit", collected)
    response = await _revert(repo, c5)
    assert response.status_code == 400
    assert response.json()["detail"]["missing_files"] == ["a.bin"]
    assert await repo.head() == c5


@history_operations_need_postgres
async def test_a_revert_whose_changes_cannot_be_read_is_still_recorded(
    m, owner_client, monkeypatch
):
    repo, (initial, c1, c2, c3, *_rest) = await _linear(m, owner_client, "revert-unread")

    async def unreadable(*args, **kwargs):
        raise RuntimeError("LakeFS unavailable")

    queued = []
    monkeypatch.setattr(m.records, "commit_changes", unreadable)
    monkeypatch.setattr(m.revert, "enqueue_lfs_reconciliation", lambda: queued.append(1))
    response = await _revert(repo, c3)
    assert response.status_code == 200, response.text
    new = response.json()["new_commit_id"]
    assert m.db.Commit.get_or_none(m.db.Commit.commit_id == new) is not None
    assert queued == [1]


async def test_a_merge_records_every_path(m, owner_client, monkeypatch):
    """Merges record what they change like commits, read to the end."""
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "merge-records")
    head = await repo.head()
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "feature", "revision": head}
    )
    assert response.status_code == 200, response.text
    await repo.commit(
        *(lfs(f"feat/w{i:03d}.bin", f"feat {i}".encode()) for i in range(150)),
        _delete("b.bin"),
        branch="feature",
    )
    monkeypatch.setattr(m.avail, "PAGE", 100)
    response = await owner_client.post(
        f"/api/models/{repo.id}/merge/feature/into/main", json={"message": "Merge feature"}
    )
    assert response.status_code == 200, response.text
    merge_commit = response.json()["result"]["reference"]
    refs = _head_refs(m, repo)
    assert len(refs) == 151 and ("b.bin", sha(b"b v1")) not in refs
    assert _files(m, repo)["b.bin"][2] is True
    H = m.db.LFSObjectHistory
    assert H.select().where(H.commit_id == merge_commit).count() == 150
    row = m.db.Commit.get(m.db.Commit.commit_id == merge_commit)
    assert row.description == "Merged feature" and row.message == "Merge feature"

    # A squash merge (one parent) is recorded against the head before it
    await repo.commit(lfs("feat/w000.bin", b"feat changed"), branch="feature")
    response = await owner_client.post(
        f"/api/models/{repo.id}/merge/feature/into/main",
        json={"message": "Squash feature", "squash_merge": True},
    )
    assert response.status_code == 200, response.text
    squashed = response.json()["result"]["reference"]
    assert H.select().where(H.commit_id == squashed).count() == 1
    assert ("feat/w000.bin", sha(b"feat changed")) in _head_refs(m, repo)
