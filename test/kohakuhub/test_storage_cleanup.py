"""Tests for storage cleanup of repositories deleted without the regular delete path."""

import asyncio
import importlib
import json
import uuid

import httpx
import pytest

from kohakuhub import lakefs_rest_client, storage_cleanup, tasks
from kohakuhub.config import cfg
from kohakuhub.db import (
    BackgroundTask,
    File,
    LFSObjectHistory,
    LfsGcCandidate,
    Repository,
    db,
)
from kohakuhub.db_operations import delete_repository
from kohakuhub.task_testing import RecordingContext, run_with_interruptions
from kohakuhub.utils.lakefs import resolve_lakefs_repo
from kohakuhub.utils.s3 import get_s3_client


def _live(module):
    """The currently registered module. The backend fixtures reload kohakuhub
    once the test environment, LakeFS credentials included, is configured, so
    a module imported at collection time can hold stale settings; anything
    that talks to the real LakeFS goes through the live module.
    """
    return importlib.import_module(module)


@pytest.fixture(autouse=True)
def clean_queue(prepared_backend_test_state):
    # This module holds the LakeFS client module imported at collection time;
    # the backend fixtures reload a fresh copy and reset only that one's pooled
    # client, which is bound to the previous test's event loop.
    lakefs_rest_client._singleton_client = None
    BackgroundTask.delete().execute()
    LfsGcCandidate.delete().execute()
    yield
    BackgroundTask.delete().execute()
    LfsGcCandidate.delete().execute()
    # Do not leave a pooled client bound to this test's loop for later modules.
    lakefs_rest_client._singleton_client = None


def _repo(full_id="owner/demo-model", repo_type="model"):
    namespace, name = full_id.split("/")
    return Repository.get(
        (Repository.repo_type == repo_type)
        & (Repository.namespace == namespace)
        & (Repository.name == name)
    )


def _queued(kind):
    return [
        json.loads(row.payload)
        for row in BackgroundTask.select()
        .where(BackgroundTask.kind == kind)
        .order_by(BackgroundTask.id)
    ]


def _lfs_shas(repo):
    return {
        row.sha256 for row in File.select().where((File.repository == repo) & (File.lfs == True))
    }


# ----- scheduling -----


def test_schedule_records_lfs_candidates_and_enqueues_the_purge_once():
    repo = _repo()
    shas = _lfs_shas(repo)
    assert shas  # the baseline model has an LFS file

    with db.atomic():
        lakefs_repo = storage_cleanup.schedule_repository_purge(repo)
        assert storage_cleanup.schedule_repository_purge(repo) == lakefs_repo  # deduplicated

    assert lakefs_repo == resolve_lakefs_repo(repo)
    assert {row.sha256 for row in LfsGcCandidate.select()} == shas
    assert _queued(storage_cleanup.PURGE_KIND) == [
        {"lakefs_repo": lakefs_repo, "repo": "model:owner/demo-model"}
    ]
    assert len(_queued(storage_cleanup.COLLECT_LFS_KIND)) == 1


def test_schedule_without_lfs_objects_skips_the_lfs_collection():
    storage_cleanup.schedule_repository_purge(_repo("acme-labs/private-dataset", "dataset"))

    assert len(_queued(storage_cleanup.PURGE_KIND)) == 1
    assert _queued(storage_cleanup.COLLECT_LFS_KIND) == []
    assert LfsGcCandidate.select().count() == 0


def test_a_failed_row_delete_rolls_back_the_scheduled_cleanup(monkeypatch):
    repo = _repo()

    def explode():
        raise RuntimeError("delete failed")

    monkeypatch.setattr(repo, "delete_instance", explode)

    with pytest.raises(RuntimeError):
        delete_repository(repo)

    assert Repository.get_or_none(Repository.id == repo.id) is not None
    assert BackgroundTask.select().count() == 0
    assert LfsGcCandidate.select().count() == 0


def test_lakefs_repo_in_use_resolves_rows_without_a_stored_id():
    repo = _repo()
    lakefs_repo = resolve_lakefs_repo(repo)
    assert storage_cleanup.lakefs_repo_in_use(lakefs_repo)
    assert not storage_cleanup.lakefs_repo_in_use("m-no-such-repo-0000")

    # A row from before migration 016 stores no id and derives the gen-0 name.
    Repository.update(lakefs_repo=None).where(Repository.id == repo.id).execute()
    try:
        assert storage_cleanup.lakefs_repo_in_use(lakefs_repo)
        assert lakefs_repo in storage_cleanup._referenced_lakefs_repos()
    finally:
        Repository.update(lakefs_repo=lakefs_repo).where(Repository.id == repo.id).execute()


# ----- purge_repository, with in-memory storage -----


class FakeStorage:
    """S3 objects and LakeFS repositories, enough for the purge and collection."""

    def __init__(self, keys=(), repos=()):
        self.keys = set(keys)
        self.repos = set(repos)

    def delete_prefix_batch(self, bucket, prefix):
        batch = sorted(key for key in self.keys if key.startswith(prefix))
        batch = batch[: storage_cleanup.S3_DELETE_BATCH]
        self.keys.difference_update(batch)
        return len(batch)

    def delete_keys(self, bucket, keys):
        self.keys.difference_update(keys)

    async def delete_repository(self, repository, force=False):
        if repository not in self.repos:
            request = httpx.Request("DELETE", f"http://lakefs/{repository}")
            raise httpx.HTTPStatusError(
                "not found", request=request, response=httpx.Response(404, request=request)
            )
        self.repos.discard(repository)


@pytest.fixture
def storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(storage_cleanup, "_delete_prefix_batch", fake.delete_prefix_batch)
    monkeypatch.setattr(storage_cleanup, "_delete_keys", fake.delete_keys)
    monkeypatch.setattr(storage_cleanup, "get_lakefs_client", lambda: fake)
    return fake


async def test_purge_deletes_the_prefix_then_the_lakefs_repository(storage, monkeypatch):
    monkeypatch.setattr(storage_cleanup, "S3_DELETE_BATCH", 2)
    doomed = "m-gone-user-model-0001"
    keep = {"m-other-model-0002/data/a", "lfs/aa/bb/aabb", "m-gone-user-model-00012/x"}

    def reset():
        storage.keys = {f"{doomed}/data/{i}" for i in range(5)} | keep
        storage.repos = {doomed, "m-other-model-0002"}

    def snapshot():
        return sorted(storage.keys), sorted(storage.repos)

    points = await run_with_interruptions(
        storage_cleanup.purge_repository,
        {"lakefs_repo": doomed, "repo": "model:gone/model"},
        reset=reset,
        snapshot=snapshot,
    )

    assert snapshot() == (sorted(keep), ["m-other-model-0002"])
    assert points == 2 * (2 + 3)  # two stages and three batch reports, two points each


async def test_purge_refuses_a_lakefs_repository_a_row_still_uses(storage):
    in_use = resolve_lakefs_repo(_repo())
    storage.keys = {f"{in_use}/data/0"}
    storage.repos = {in_use}

    with pytest.raises(tasks.PermanentTaskError, match="in use"):
        await storage_cleanup.purge_repository(
            {"lakefs_repo": in_use, "repo": "model:owner/demo-model"}, RecordingContext()
        )

    assert storage.keys == {f"{in_use}/data/0"} and storage.repos == {in_use}


async def test_purge_treats_an_already_deleted_lakefs_repository_as_done(storage):
    # A run that crashed right after deleting the LakeFS repository reruns.
    storage.keys = {"m-gone-0004/data/0"}
    storage.repos = set()

    await storage_cleanup.purge_repository(
        {"lakefs_repo": "m-gone-0004", "repo": "model:gone/y"}, RecordingContext()
    )

    assert storage.keys == set()


async def test_purge_surfaces_lakefs_errors_other_than_not_found(storage):
    async def unavailable(repository, force=False):
        request = httpx.Request("DELETE", f"http://lakefs/{repository}")
        raise httpx.HTTPStatusError(
            "unavailable", request=request, response=httpx.Response(503, request=request)
        )

    storage.delete_repository = unavailable

    with pytest.raises(httpx.HTTPStatusError):
        await storage_cleanup.purge_repository(
            {"lakefs_repo": "m-gone-0003", "repo": "model:gone/x"}, RecordingContext()
        )


# ----- collect_lfs -----


def _history(repo, sha):
    LFSObjectHistory.create(
        repository=repo, path_in_repo="old/weights.bin", sha256=sha, size=1, commit_id="c0ffee"
    )


async def test_collect_deletes_only_lfs_objects_nothing_references(storage, monkeypatch):
    monkeypatch.setattr(storage_cleanup, "LFS_BATCH", 2)
    repo = _repo()
    (live,) = _lfs_shas(repo)  # an active LFS file still uses it
    historic = "b" * 64  # only an LFS history row of another repository uses it
    orphans = ["c" * 64, "d" * 64]
    _history(repo, historic)
    candidates = [live, historic, *orphans]

    def reset():
        storage.keys = {storage_cleanup.lfs_key(sha) for sha in candidates}
        storage_cleanup.record_lfs_candidates(candidates)

    def snapshot():
        return sorted(storage.keys), LfsGcCandidate.select().count()

    try:
        points = await run_with_interruptions(
            storage_cleanup.collect_lfs, {}, reset=reset, snapshot=snapshot
        )
    finally:
        LFSObjectHistory.delete().where(LFSObjectHistory.sha256 == historic).execute()

    kept = sorted(storage_cleanup.lfs_key(sha) for sha in (live, historic))
    assert snapshot() == (kept, 0)
    assert points == 2 + 2 * 2  # a stage and two batch reports


async def test_collect_with_nothing_recorded_is_a_no_op(storage):
    ctx = RecordingContext()
    await storage_cleanup.collect_lfs({}, ctx)
    assert ctx.reports == []


# ----- the S3 helpers against the real bucket -----


def test_s3_helpers_delete_in_batches_and_report_refusals(monkeypatch):
    monkeypatch.setattr(storage_cleanup, "S3_DELETE_BATCH", 2)
    client = get_s3_client()
    prefix = f"test-cleanup-{uuid.uuid4().hex[:8]}/"
    for i in range(3):
        client.put_object(Bucket=cfg.s3.bucket, Key=f"{prefix}{i}", Body=b"x")
    client.put_object(Bucket=cfg.s3.bucket, Key=f"{prefix}lfs-copy", Body=b"y")

    storage_cleanup._delete_keys(cfg.s3.bucket, [f"{prefix}lfs-copy"])
    assert storage_cleanup._delete_prefix_batch(cfg.s3.bucket, prefix) == 2
    assert storage_cleanup._delete_prefix_batch(cfg.s3.bucket, prefix) == 1
    assert storage_cleanup._delete_prefix_batch(cfg.s3.bucket, prefix) == 0

    with pytest.raises(RuntimeError, match="refused to delete 1 object"):
        storage_cleanup._check_delete_errors(
            {"Errors": [{"Key": "k", "Code": "AccessDenied", "Message": "no"}]}
        )
    storage_cleanup._check_delete_errors({})


# ----- orphan audit against the real LakeFS -----


@pytest.fixture
async def orphan():
    name = f"m-orphan-{uuid.uuid4().hex[:10]}"
    client = _live("kohakuhub.utils.lakefs").get_lakefs_client()
    await client.create_repository(name=name, storage_namespace=f"s3://{cfg.s3.bucket}/{name}")
    yield name
    try:
        await client.delete_repository(repository=name, force=True)
    except httpx.HTTPStatusError:
        pass


async def test_find_orphans_lists_unreferenced_lakefs_repositories(orphan, monkeypatch):
    live = _live("kohakuhub.storage_cleanup")
    monkeypatch.setattr(live, "LAKEFS_LIST_PAGE", 1)  # exercise pagination
    referenced = storage_cleanup._referenced_lakefs_repos()

    found = {entry["id"]: entry for entry in await live.find_orphan_lakefs_repositories()}

    assert orphan in found
    assert not referenced & set(found)
    assert found[orphan]["storage_namespace"] == f"s3://{cfg.s3.bucket}/{orphan}"
    assert found[orphan]["purge_pending"] is False

    storage_cleanup.enqueue_purge(orphan, f"orphan:{orphan}")
    corrupt = storage_cleanup.enqueue_purge("m-corrupt-0000", "orphan:m-corrupt-0000")
    BackgroundTask.update(payload="{broken").where(BackgroundTask.id == corrupt).execute()
    found = {entry["id"]: entry for entry in await live.find_orphan_lakefs_repositories()}
    assert found[orphan]["purge_pending"] is True


async def test_admin_can_review_and_purge_orphans(admin_client, orphan):
    listing = (await admin_client.get("/admin/api/storage/orphans")).json()
    assert orphan in {entry["id"] for entry in listing["orphans"]}
    assert listing["count"] == len(listing["orphans"])

    first = await admin_client.post(f"/admin/api/storage/orphans/{orphan}/purge")
    assert first.status_code == 200
    assert first.json()["already_pending"] is False
    again = (await admin_client.post(f"/admin/api/storage/orphans/{orphan}/purge")).json()
    assert again["already_pending"] is True and again["task_id"] is None

    in_use = resolve_lakefs_repo(_repo())
    refused = await admin_client.post(f"/admin/api/storage/orphans/{in_use}/purge")
    assert refused.status_code == 409
    missing = await admin_client.post("/admin/api/storage/orphans/m-missing-0000/purge")
    assert missing.status_code == 404

    (payload,) = _queued(storage_cleanup.PURGE_KIND)
    await _live("kohakuhub.storage_cleanup").purge_repository(payload, RecordingContext())
    assert await _gone(orphan)


async def _gone(lakefs_repo, timeout=30.0):
    """LakeFS deletes repositories asynchronously (#93); wait for it."""
    client = _live("kohakuhub.utils.lakefs").get_lakefs_client()
    deadline = asyncio.get_running_loop().time() + timeout
    while await client.repository_exists(lakefs_repo):
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.5)
    return True


async def test_admin_repository_details_show_the_lakefs_repository(admin_client):
    body = (await admin_client.get("/admin/api/repositories/model/owner/demo-model")).json()
    assert body["lakefs_repo"] == resolve_lakefs_repo(_repo())
