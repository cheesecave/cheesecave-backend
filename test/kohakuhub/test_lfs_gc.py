"""LFS garbage collection against the real database and bucket (#114)."""

import hashlib
import importlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from kohakuhub import lakefs_rest_client
from kohakuhub.task_testing import RecordingContext
from test.kohakuhub.api.helpers import encode_ndjson

PATH = "gc-test/weights.bin"


def _live(module):
    """The currently registered module (see test_storage_cleanup.py)."""
    return importlib.import_module(module)


@pytest.fixture
def m(prepared_backend_test_state):
    lakefs_rest_client._singleton_client = None
    ns = SimpleNamespace(
        gc=_live("kohakuhub.lfs_gc"),
        db=_live("kohakuhub.db"),
        cleanup=_live("kohakuhub.storage_cleanup"),
        cfg=_live("kohakuhub.config").cfg,
        s3=_live("kohakuhub.utils.s3").get_s3_client(),
    )
    state = (
        ns.db.BackgroundTask,
        ns.db.LfsGcCandidate,
        ns.db.LfsGcState,
        ns.db.LfsObjectTombstone,
        ns.db.LfsRecentObject,
    )
    for model in state:
        model.delete().execute()
    keep = {repo.id: repo.lfs_keep_versions for repo in ns.db.Repository.select()}
    yield ns
    for model in state:
        model.delete().execute()
    H = ns.db.LFSObjectHistory
    H.delete().where(H.path_in_repo.startswith("gc-test/")).execute()
    for repo_id, value in keep.items():
        ns.db.Repository.update(lfs_keep_versions=value).where(
            ns.db.Repository.id == repo_id
        ).execute()
    lakefs_rest_client._singleton_client = None


def _repo(m, full_id="owner/demo-model", repo_type="model"):
    namespace, name = full_id.split("/")
    return m.db.Repository.get(
        (m.db.Repository.repo_type == repo_type)
        & (m.db.Repository.namespace == namespace)
        & (m.db.Repository.name == name)
    )


def _sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


def _history(m, repo, shas, path=PATH):
    """History rows for ``shas``, oldest first."""
    start = m.db.utcnow() - timedelta(hours=len(shas))
    for age, sha in enumerate(shas):
        m.db.LFSObjectHistory.create(
            repository=repo,
            path_in_repo=path,
            sha256=sha,
            size=10,
            commit_id=f"c{age}",
            created_at=start + timedelta(hours=age),
        )


def _age_recent(m):
    m.db.LfsRecentObject.update(touched_at=m.db.utcnow() - timedelta(days=2)).execute()


def _candidates(m):
    return {row.sha256 for row in m.db.LfsGcCandidate.select()}


def _queued(m, kind):
    B = m.db.BackgroundTask
    return [json.loads(row.payload) for row in B.select().where(B.kind == kind).order_by(B.id)]


def _stored(m, sha):
    try:
        m.s3.head_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(sha))
        return True
    except ClientError:
        return False


def _put(m, content):
    oid = hashlib.sha256(content).hexdigest()
    m.s3.put_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(oid), Body=content)
    return oid


async def _commit(client, repo, operation, summary="gc test"):
    return await client.post(
        f"/api/models/owner/{repo}/commit/main",
        content=encode_ndjson(
            [{"key": "header", "value": {"summary": summary, "description": ""}}, operation]
        ),
        headers={"Content-Type": "application/x-ndjson"},
    )


def _lfs_op(content, path=PATH):
    oid = hashlib.sha256(content).hexdigest()
    return {
        "key": "lfsFile",
        "value": {"path": path, "oid": oid, "size": len(content), "algo": "sha256"},
    }


# ----- the retention decision -----


def test_retention_reasons(m, monkeypatch):
    repo = _repo(m)
    other = _repo(m, "acme-labs/private-dataset", "dataset")
    live = m.db.File.get((m.db.File.repository == repo) & (m.db.File.lfs == True)).sha256
    v1, v2, v3 = _sha("v1"), _sha("v2"), _sha("v3")
    _history(m, repo, [v1, v2, v3, v3])  # v3 committed twice: one unique version
    repo.lfs_keep_versions = 2
    repo.save()

    assert m.gc.retention_reason(_sha("unknown")) is None
    assert m.gc.retention_reason(live) == "file"
    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", False)
    assert m.gc.retention_reason(v1) == "history"  # every version is kept

    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    assert [m.gc.retention_reason(sha) for sha in (v1, v2, v3)] == [None, "history", "history"]
    assert m.gc.evicted_versions(repo, PATH) == [v1]

    # The same content inside another repository's window is kept
    _history(m, other, [v1], path="gc-test/shared.bin")
    assert m.gc.retention_reason(v1) == "history"

    m.gc.touch(v3)
    assert m.gc.retention_reason(v3) == "recent"
    later = m.db.utcnow() + m.gc.RECENT_GRACE + timedelta(minutes=1)
    assert m.gc.retention_reason(v3, now=later) == "history"


def test_repository_keep_count_falls_back_to_the_server_default(m, monkeypatch):
    repo = _repo(m)
    monkeypatch.setattr(m.cfg.app, "lfs_keep_versions", 4)
    repo.lfs_keep_versions = None
    assert m.gc.keep_versions(repo) == 4
    assert _live("kohakuhub.db_operations").get_effective_lfs_keep_versions(repo) == 4
    repo.lfs_keep_versions = 3
    assert m.gc.keep_versions(repo) == 3


def test_evicted_versions_are_recorded_only_with_auto_gc(m, monkeypatch):
    repo = _repo(m)
    v1, v2, v3 = _sha("v1"), _sha("v2"), _sha("v3")
    _history(m, repo, [v1, v2, v3])
    repo.lfs_keep_versions = 2

    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", False)
    assert m.gc.record_evicted_versions(repo, [PATH]) == 0
    assert _candidates(m) == set()

    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    assert m.gc.record_evicted_versions(repo, [PATH, PATH]) == 1
    assert _candidates(m) == {v1}


def test_candidates_are_recorded_in_batches_without_duplicates(m, monkeypatch):
    monkeypatch.setattr(m.gc, "CANDIDATE_BATCH", 2)
    shas = [_sha(f"c{i}") for i in range(5)]
    assert m.gc.record_candidates(shas + shas[:2]) == 5
    assert m.gc.record_candidates(shas[:1]) == 1  # already recorded: ignored
    assert _candidates(m) == set(shas)


# ----- claims, tombstones and races -----


def test_a_commit_claim_protects_revives_or_refuses(m):
    T = m.db.LfsObjectTombstone
    fresh, deleting, deleted = _sha("fresh"), _sha("deleting"), _sha("deleted")
    T.create(sha256=deleting, state=m.gc.DELETING)
    T.create(sha256=deleted, state=m.gc.DELETED)

    assert m.gc.claim_for_commit(fresh, exists_in_storage=True) is False
    assert m.gc.retention_reason(fresh) == "recent"

    with pytest.raises(m.gc.LfsObjectUnavailable):
        m.gc.claim_for_commit(_sha("missing"), exists_in_storage=False)
    with pytest.raises(m.gc.LfsObjectUnavailable):
        m.gc.claim_for_commit(deleting, exists_in_storage=True)
    assert m.gc.tombstone_state(deleting) == m.gc.DELETING

    # Collected, then uploaded again: the claim revives it
    assert m.gc.claim_for_commit(deleted, exists_in_storage=True) is True
    assert m.gc.tombstone_state(deleted) is None
    assert m.gc.retention_reason(deleted) == "recent"


def test_begin_delete_decides_under_the_lock_and_finish_delete_completes(m):
    orphan, claimed = _sha("orphan"), _sha("claimed")

    assert m.gc.begin_delete(orphan) is True
    assert m.gc.tombstone_state(orphan) == m.gc.DELETING
    assert m.gc.begin_delete(orphan) is True  # a rerun after a crash

    # A claim that won the race keeps the object and clears the leftover
    m.gc.begin_delete(claimed)
    m.gc.touch(claimed)
    assert m.gc.begin_delete(claimed) is False
    assert m.gc.tombstone_state(claimed) is None

    m.gc.finish_delete([])
    m.gc.finish_delete([orphan])
    assert m.gc.tombstone_state(orphan) == m.gc.DELETED
    assert m.gc.deleted_shas([orphan, claimed]) == {orphan}


def test_deleted_shas_looks_up_in_batches(m, monkeypatch):
    monkeypatch.setattr(m.gc, "CANDIDATE_BATCH", 2)
    shas = [_sha(f"d{i}") for i in range(5)]
    for sha in shas[:3]:
        m.db.LfsObjectTombstone.create(sha256=sha, state=m.gc.DELETED)
    assert m.gc.deleted_shas(shas) == set(shas[:3])
    assert m.gc.deleted_shas([]) == set()


def test_the_object_lock_is_skipped_off_postgres(m, monkeypatch):
    calls = []
    monkeypatch.setattr(m.gc, "_database", lambda: SimpleNamespace(execute_sql=calls.append))
    m.gc._lock(_sha("x"))
    assert calls == []


# ----- background tasks -----


async def test_expired_uploads_become_candidates(m, monkeypatch):
    old = [_sha(f"old{i}") for i in range(3)]
    recent = _sha("recent")
    for sha in old:
        m.gc.touch(sha, now=m.db.utcnow() - timedelta(days=2))
    m.gc.touch(recent)

    assert m.gc.expire_recent(batch=2) == 3
    assert _candidates(m) == set(old)
    assert {row.sha256 for row in m.db.LfsRecentObject.select()} == {recent}

    # The hourly task hands expired uploads to the collection
    await m.cleanup.expire_recent_lfs({})
    assert _queued(m, m.cleanup.COLLECT_LFS_KIND) == []
    m.gc.touch(_sha("old3"), now=m.db.utcnow() - timedelta(days=2))
    await m.cleanup.expire_recent_lfs({})
    assert len(_queued(m, m.cleanup.COLLECT_LFS_KIND)) == 1


async def test_review_records_versions_a_lowered_keep_count_evicts(m, monkeypatch):
    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    repo = _repo(m)
    v = [_sha(f"r{i}") for i in range(4)]
    _history(m, repo, v)
    _history(m, repo, v[:2], path="gc-test/other.bin")
    ctx = RecordingContext()

    await m.cleanup.review_lfs_window({"repo_id": repo.id}, ctx)
    assert _candidates(m) == set()  # the server default keeps five
    assert _queued(m, m.cleanup.COLLECT_LFS_KIND) == []

    repo.lfs_keep_versions = 2
    repo.save()
    await m.cleanup.review_lfs_window({"repo_id": repo.id}, ctx)
    assert _candidates(m) == set(v[:2])
    assert len(_queued(m, m.cleanup.COLLECT_LFS_KIND)) == 1
    assert ctx.reports[-1] == (3, 3)  # both test paths and the seeded weights

    await m.cleanup.review_lfs_window({"repo_id": -1}, RecordingContext())  # deleted since


async def test_lowering_the_keep_count_schedules_a_review(m, owner_client, monkeypatch):
    repo = _repo(m)
    kind = m.cleanup.REVIEW_LFS_WINDOW_KIND

    async def put(keep):
        response = await owner_client.put(
            "/api/models/owner/demo-model/settings", json={"lfs_keep_versions": keep}
        )
        assert response.status_code == 200

    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", False)
    await put(3)
    assert _queued(m, kind) == []  # every version is kept anyway

    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    await put(4)  # raised
    assert _queued(m, kind) == []
    await put(2)
    await put(2)  # unchanged
    assert _queued(m, kind) == [{"repo_id": repo.id}]


# ----- end to end through the API -----


async def test_commits_collect_evicted_versions_and_keep_shared_content(
    m, owner_client, monkeypatch
):
    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    repo = _repo(m)
    repo.lfs_keep_versions = 2
    repo.save()
    versions = [f"gc version {i}".encode() for i in range(4)]
    oids = [_put(m, content) for content in versions]

    for content in versions[:3]:
        assert (await _commit(owner_client, "demo-model", _lfs_op(content))).status_code == 200
    assert _candidates(m) == {oids[0]}
    assert len(_queued(m, m.cleanup.COLLECT_LFS_KIND)) == 1

    # Version 0 is also the current content of another path: kept
    op = _lfs_op(versions[0], path="gc-test/copy.bin")
    assert (await _commit(owner_client, "demo-model", op)).status_code == 200
    assert (await _commit(owner_client, "demo-model", _lfs_op(versions[3]))).status_code == 200
    assert _candidates(m) == {oids[0], oids[1]}

    _age_recent(m)  # the uploads' grace period is over
    m.gc.mark_references_reconciled()
    await m.cleanup.collect_lfs({}, RecordingContext())

    assert [_stored(m, oid) for oid in oids] == [True, False, True, True]
    assert m.gc.tombstone_state(oids[1]) == m.gc.DELETED
    assert _candidates(m) == set()

    # History rows stay; recoverability and quota ask the tombstones
    commit_id = m.db.LFSObjectHistory.get(
        (m.db.LFSObjectHistory.repository == repo) & (m.db.LFSObjectHistory.sha256 == oids[1])
    ).commit_id
    gc_utils = _live("kohakuhub.api.repo.utils.gc")
    recoverable, missing = await gc_utils.check_lfs_recoverability(repo, commit_id)
    assert (recoverable, missing) == (False, [PATH])
    storage = await _live("kohakuhub.api.quota.util").calculate_repository_storage(repo)
    H = m.db.LFSObjectHistory
    live_bytes = sum(
        row.size for row in H.select().where((H.repository == repo) & (H.sha256 != oids[1]))
    )
    assert storage["lfs_total_bytes"] == live_bytes


async def test_collected_content_must_be_uploaded_again(m, owner_client):
    content = b"gc collected payload"
    oid = _put(m, content)
    m.gc.begin_delete(oid)
    m.gc.finish_delete([oid])  # collected, though still in storage for a moment
    batch_url = "/models/owner/demo-model.git/info/lfs/objects/batch"

    async def batch(operation):
        response = await owner_client.post(
            batch_url,
            json={"operation": operation, "objects": [{"oid": oid, "size": len(content)}]},
        )
        assert response.status_code == 200
        return response.json()["objects"][0]

    # The upload is not skipped, and the object is protected from now on
    assert "upload" in (await batch("upload")).get("actions", {})
    assert m.gc.retention_reason(oid) == "recent"

    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(oid))
    response = await _commit(owner_client, "demo-model", _lfs_op(content))
    assert response.status_code == 400  # not in storage at all

    m.db.LfsObjectTombstone.update(state=m.gc.DELETING).execute()
    _put(m, content)
    response = await _commit(owner_client, "demo-model", _lfs_op(content))
    assert response.status_code == 409  # a collection is deleting it
    assert "Upload it again" in response.text

    # Uploaded again after the collection finished: the commit revives it
    m.gc.finish_delete([oid])
    assert (await _commit(owner_client, "demo-model", _lfs_op(content))).status_code == 200
    assert m.gc.tombstone_state(oid) is None
    assert (await batch("upload")).get("actions") is None  # exists: skip
    assert "download" in (await batch("download"))["actions"]


async def test_restoring_a_deleted_file_claims_its_object(m, owner_client):
    content = b"gc restored payload"
    _put(m, content)
    delete = {"key": "deletedFile", "value": {"path": PATH}}
    assert (await _commit(owner_client, "demo-model", _lfs_op(content))).status_code == 200
    assert (await _commit(owner_client, "demo-model", delete)).status_code == 200
    oid = hashlib.sha256(content).hexdigest()

    # Collected while the file was deleted: restoring must upload it again
    m.db.LfsRecentObject.delete().execute()
    m.gc.begin_delete(oid)
    m.gc.finish_delete([oid])
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(oid))
    response = await _commit(owner_client, "demo-model", _lfs_op(content))
    assert response.status_code == 409

    _put(m, content)
    assert (await _commit(owner_client, "demo-model", _lfs_op(content))).status_code == 200
    assert m.gc.tombstone_state(oid) is None
    assert m.gc.retention_reason(oid) == "recent"

    # A download of collected content is refused, even with a File row
    m.gc.begin_delete(oid)  # kept: the file is active again
    m.db.LfsObjectTombstone.create(sha256=oid, state=m.gc.DELETED)
    response = await owner_client.post(
        "/models/owner/demo-model.git/info/lfs/objects/batch",
        json={"operation": "download", "objects": [{"oid": oid, "size": len(content)}]},
    )
    assert response.json()["objects"][0]["error"]["code"] == 404


# ----- reconciling what branch heads link -----


def test_lfs_oid_accepts_only_global_lfs_addresses(m):
    sha = _sha("oid")
    assert m.gc.lfs_oid(f"s3://hub-storage/{m.gc.lfs_key(sha)}") == sha
    assert m.gc.lfs_oid(f"s3://hub-storage/m-repo-0001/data/{sha}") is None
    assert m.gc.lfs_oid(f"s3://hub-storage/lfs/ab/cd/{sha[:32]}") is None  # an MD5 is no oid
    assert m.gc.lfs_oid("") is None
    assert m.gc.lfs_oid(None) is None


def test_active_files_keep_content_whatever_their_lfs_flag(m):
    F = m.db.File
    row = F.get((F.repository == _repo(m)) & (F.lfs == True))
    F.update(lfs=False).where(F.id == row.id).execute()  # revert under a raised threshold
    try:
        assert m.gc.retention_reason(row.sha256) == "file"
    finally:
        F.update(lfs=True).where(F.id == row.id).execute()


def test_reconcile_references_adds_and_corrects_only_what_is_missing(m):
    repo = _repo(m)
    repo.lfs_keep_versions = 2
    F = m.db.File
    v = [_sha(f"h{i}") for i in range(4)]
    _history(m, repo, v)  # v3 and v2 are in the window, v1 and v0 are not
    wrong = F.create(
        repository=repo,
        path_in_repo="gc-test/copied.bin",
        sha256=v[3],
        size=1,
        lfs=True,
        owner=repo.owner_id,
    )
    unflagged = F.create(
        repository=repo,
        path_in_repo="gc-test/unflagged.bin",
        sha256=v[2],
        size=10,
        lfs=False,
        owner=repo.owner_id,
    )
    heads = {
        PATH: {v[3]: (10, "main-head"), v[0]: (10, "dev-head")},  # dev still links v0
        "gc-test/copied.bin": {v[1]: (10, "main-head")},  # copyFile of an older version
        "gc-test/unflagged.bin": {v[2]: (10, "main-head")},
        "gc-test/new.bin": {v[2]: (10, "main-head")},
    }
    default_head = {
        "gc-test/copied.bin": (v[1], 10),
        "gc-test/unflagged.bin": (v[2], 10),
        "gc-test/new.bin": (v[2], 10),
    }
    try:
        assert m.gc.reconcile_references(repo, heads, default_head) == {
            "history_added": 4,  # v0 on PATH, and the three paths without history
            "files_fixed": 3,
        }
        assert m.gc.evicted_versions(repo, PATH) == [v[3], v[2], v[1]][1:]  # v0 is newest now
        assert m.gc._unique_versions(repo.id, PATH)[:2] == [v[0], v[3]]
        rows = {
            row.path_in_repo: (row.sha256, row.size, row.lfs, row.is_deleted)
            for row in F.select().where(F.path_in_repo.in_(list(default_head)))
        }
        assert rows == {
            "gc-test/copied.bin": (v[1], 10, True, False),
            "gc-test/unflagged.bin": (v[2], 10, True, False),
            "gc-test/new.bin": (v[2], 10, True, False),
        }
        # Everything accounts now: running it again changes nothing
        assert m.gc.reconcile_references(repo, heads, default_head) == {
            "history_added": 0,
            "files_fixed": 0,
        }
    finally:
        F.delete().where(
            F.id.in_([wrong.id, unflagged.id]) | (F.path_in_repo == "gc-test/new.bin")
        ).execute()


async def test_collection_waits_for_the_reconciliation_with_auto_gc(m, monkeypatch):
    orphan = _sha("waiting")
    m.gc.record_candidates([orphan])

    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", True)
    await m.cleanup.collect_lfs({}, RecordingContext())
    assert _candidates(m) == {orphan}  # nothing decided yet
    assert len(_queued(m, m.cleanup.RECONCILE_LFS_KIND)) == 1
    await m.cleanup.collect_lfs({}, RecordingContext())
    assert len(_queued(m, m.cleanup.RECONCILE_LFS_KIND)) == 1  # deduplicated

    # Without auto GC keep windows decide nothing, so there is nothing to wait for
    monkeypatch.setattr(m.cfg.app, "lfs_auto_gc", False)
    await m.cleanup.collect_lfs({}, RecordingContext())
    assert _candidates(m) == set()
    assert m.gc.tombstone_state(orphan) == m.gc.DELETED


def test_the_reconciled_marker_records_the_latest_run(m):
    assert (m.gc.references_reconciled(), m.gc.reconciled_at()) == (False, None)
    m.gc.mark_references_reconciled()
    first = m.gc.reconciled_at()
    m.gc.mark_references_reconciled()
    assert m.gc.references_reconciled() and m.gc.reconciled_at() >= first


async def _branch(client, name, revision="main"):
    response = await client.post(
        "/api/models/owner/demo-model/branch", json={"branch": name, "revision": revision}
    )
    assert response.status_code == 200, response.text


async def test_branch_heads_are_listed_page_by_page(m, owner_client, monkeypatch):
    content = b"gc dev branch payload"
    _put(m, content)
    # File rows have no branch: the dev commit below writes one too
    live = m.db.File.get((m.db.File.repository == _repo(m)) & (m.db.File.lfs == True))
    await _branch(owner_client, "gc-dev")
    await _branch(owner_client, "zz-gc-same")  # same commit as main: listed once
    op = _lfs_op(content, path="gc-test/dev.bin")
    response = await owner_client.post(
        "/api/models/owner/demo-model/commit/gc-dev",
        content=encode_ndjson([{"key": "header", "value": {"summary": "dev"}}, op]),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert response.status_code == 200
    monkeypatch.setattr(m.cleanup, "LAKEFS_LIST_PAGE", 1)
    lakefs_repo = _live("kohakuhub.utils.lakefs").resolve_lakefs_repo(_repo(m))

    heads, default_head, counts = await m.cleanup.branch_head_references(lakefs_repo)

    oid = hashlib.sha256(content).hexdigest()
    assert list(heads["gc-test/dev.bin"]) == [oid]  # only on the dev branch
    assert "gc-test/dev.bin" not in default_head
    assert default_head[live.path_in_repo] == (live.sha256, live.size)
    # main, gc-dev (main's objects plus dev.bin); zz-gc-same shares main's commit
    assert counts["branches"] == 3
    assert counts["lfs_references"] == 2 * len(default_head) + 1
    assert await m.cleanup.branch_head_references("m-gc-missing-0000") is None


async def test_branch_head_listing_surfaces_lakefs_errors(m, monkeypatch):
    class Unavailable:
        async def get_repository(self, repository):
            import httpx

            request = httpx.Request("GET", f"http://lakefs/{repository}")
            raise httpx.HTTPStatusError(
                "unavailable", request=request, response=httpx.Response(503, request=request)
            )

    monkeypatch.setattr(m.cleanup, "get_lakefs_client", lambda: Unavailable())
    with pytest.raises(Exception, match="unavailable"):
        await m.cleanup.branch_head_references("m-any")


async def test_the_reconciliation_converges_however_it_is_interrupted(m, owner_client):
    repo = _repo(m)
    F, H = m.db.File, m.db.LFSObjectHistory
    live = F.get((F.repository == repo) & (F.lfs == True))
    baseline = {row.id for row in H.select(H.id)}
    ghost = m.db.Repository.create(
        repo_type="model",
        namespace="owner",
        name="gc-ghost",
        full_id="owner/gc-ghost",
        lakefs_repo="m-gc-ghost-0000",
        owner=repo.owner_id,
    )

    def reset():
        # Data written by an earlier version: a wrong file row, no history
        H.delete().where(H.id.not_in(baseline) | (H.sha256 == live.sha256)).execute()
        F.update(sha256="0" * 64, lfs=False).where(F.id == live.id).execute()
        m.db.LfsGcState.delete().execute()
        m.db.BackgroundTask.delete().execute()

    def snapshot():
        return (
            sorted((row.repository_id, row.path_in_repo, row.sha256) for row in H.select()),
            F.get_by_id(live.id).sha256,
            F.get_by_id(live.id).lfs,
            m.gc.references_reconciled(),
            len(_queued(m, m.cleanup.COLLECT_LFS_KIND)),
        )

    try:
        points = await _live("kohakuhub.task_testing").run_with_interruptions(
            m.cleanup.reconcile_lfs_references, {}, reset=reset, snapshot=snapshot
        )
        history, sha, lfs, reconciled, collections = snapshot()
        assert (sha, lfs, reconciled, collections) == (live.sha256, True, True, 1)
        assert (repo.id, live.path_in_repo, live.sha256) in history
        assert points == 6 * m.db.Repository.select().count()  # stage, checkpoint, progress

        # Over data that already accounts, a run changes nothing
        ctx = RecordingContext()
        await m.cleanup.reconcile_lfs_references({}, ctx)
        stats = ctx.checkpoint_state["stats"]
        assert (stats["history_added"], stats["files_fixed"]) == (0, 0)
        assert stats["repositories_without_lakefs"] == 1
        assert snapshot()[0] == history

        cancelled = RecordingContext()
        cancelled.cancel_requested = True
        with pytest.raises(_live("kohakuhub.tasks").TaskCancelled):
            await m.cleanup.reconcile_lfs_references({}, cancelled)
    finally:
        ghost.delete_instance()
        H.delete().where(H.id.not_in(baseline)).execute()
        F.update(sha256=live.sha256, lfs=True).where(F.id == live.id).execute()


async def test_admins_start_and_follow_the_reconciliation(m, admin_client):
    url = "/admin/api/storage/lfs-reconciliation"
    status = (await admin_client.get(url)).json()
    assert status == {"reconciled_at": None, "auto_gc": m.cfg.app.lfs_auto_gc, "task": None}

    started = (await admin_client.post(url)).json()
    assert started["already_pending"] is False
    assert (await admin_client.post(url)).json() == {"task_id": None, "already_pending": True}
    task = (await admin_client.get(url)).json()["task"]
    assert (task["id"], task["status"], task["stats"], task["finished_at"]) == (
        started["task_id"],
        "queued",
        {},
        None,
    )

    B = m.db.BackgroundTask
    B.update(
        status="succeeded",
        finished_at=m.db.utcnow(),
        checkpoint=json.dumps({"after": 1, "stats": {"history_added": 2}}),
    ).where(B.id == started["task_id"]).execute()
    m.gc.mark_references_reconciled()
    status = (await admin_client.get(url)).json()
    assert status["reconciled_at"] and status["task"]["stats"] == {"history_added": 2}
    assert status["task"]["finished_at"]


async def test_copying_an_older_version_records_what_is_linked(m, owner_client):
    old, new = b"gc copy v0", b"gc copy v1"
    _put(m, old), _put(m, new)
    assert (await _commit(owner_client, "demo-model", _lfs_op(old))).status_code == 200
    first = (await owner_client.get("/api/models/owner/demo-model/revision/main")).json()["sha"]
    assert (await _commit(owner_client, "demo-model", _lfs_op(new))).status_code == 200

    copy = {
        "key": "copyFile",
        "value": {"path": "gc-test/copy.bin", "srcPath": PATH, "srcRevision": first},
    }
    assert (await _commit(owner_client, "demo-model", copy)).status_code == 200

    oid = hashlib.sha256(old).hexdigest()
    F, H = m.db.File, m.db.LFSObjectHistory
    row = F.get((F.repository == _repo(m)) & (F.path_in_repo == "gc-test/copy.bin"))
    assert (row.sha256, row.lfs) == (oid, True)  # not the source's current version
    assert H.select().where((H.path_in_repo == "gc-test/copy.bin") & (H.sha256 == oid)).exists()
    assert m.gc.retention_reason(oid) == "recent"  # claimed like a linked upload
