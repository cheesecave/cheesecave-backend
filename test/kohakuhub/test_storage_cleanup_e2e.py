"""End to end: deleting owners cleans their repositories' storage (#109).

Each test deletes baseline rows, so each restores the baseline first.
"""

import asyncio
import importlib
import json

import pytest
from botocore.exceptions import ClientError

from kohakuhub import lakefs_rest_client, storage_cleanup, tasks
from kohakuhub.config import cfg
from kohakuhub.db import BackgroundTask, File, LfsGcCandidate, Repository, User
from kohakuhub.db_operations import delete_organization, delete_repository
from kohakuhub.utils.lakefs import resolve_lakefs_repo
from kohakuhub.utils.s3 import get_s3_client

pytestmark = pytest.mark.backend_per_test


def _live(module):
    """The currently registered module. The backend fixtures reload kohakuhub
    once the test environment, LakeFS credentials included, is configured, so
    a module imported at collection time can hold stale settings; anything
    that talks to the real LakeFS goes through the live module.
    """
    return importlib.import_module(module)


@pytest.fixture(autouse=True)
def clean_queue(prepared_backend_test_state):
    lakefs_rest_client._singleton_client = None  # see test_storage_cleanup.py
    BackgroundTask.delete().execute()
    LfsGcCandidate.delete().execute()
    yield
    BackgroundTask.delete().execute()
    LfsGcCandidate.delete().execute()
    # Do not leave a pooled client bound to this test's loop for later modules.
    lakefs_rest_client._singleton_client = None


def _prefix_keys(lakefs_repo):
    listing = get_s3_client().list_objects_v2(Bucket=cfg.s3.bucket, Prefix=f"{lakefs_repo}/")
    return [item["Key"] for item in listing.get("Contents", [])]


def _object_exists(key):
    try:
        get_s3_client().head_object(Bucket=cfg.s3.bucket, Key=key)
        return True
    except ClientError:
        return False


async def _drain_storage_tasks(timeout=60.0):
    """Run a real worker until every storage cleanup task has finished."""
    kinds = (storage_cleanup.PURGE_KIND, storage_cleanup.COLLECT_LFS_KIND)

    def pending():
        return (
            BackgroundTask.select()
            .where(
                BackgroundTask.kind.in_(kinds)
                & BackgroundTask.status.in_([tasks.QUEUED, tasks.RUNNING])
            )
            .exists()
        )

    stop = asyncio.Event()
    worker = _live("kohakuhub.worker").Worker(
        worker_id="w-cleanup", concurrency=2, lease_seconds=30, poll_interval=0.05
    )
    runner = asyncio.create_task(worker.run(stop))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while pending():
            assert loop.time() < deadline, "storage cleanup did not finish"
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await runner
    return {
        row.kind: row.status
        for row in BackgroundTask.select().where(BackgroundTask.kind.in_(kinds))
    }


async def _gone(lakefs_repo, timeout=30.0):
    """LakeFS deletes repositories asynchronously (#93); wait for it."""
    client = _live("kohakuhub.utils.lakefs").get_lakefs_client()
    deadline = asyncio.get_running_loop().time() + timeout
    while await client.repository_exists(lakefs_repo):
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.5)
    return True


async def test_force_deleting_a_user_cleans_their_storage(admin_client):
    owner = User.get(User.username == "owner")
    repos = list(Repository.select().where(Repository.owner == owner))
    lakefs_repos = [resolve_lakefs_repo(repo) for repo in repos]
    lfs_shas = {
        row.sha256 for row in File.select().where(File.repository.in_(repos) & (File.lfs == True))
    }
    assert repos and lfs_shas
    assert all(
        [
            await _live("kohakuhub.utils.lakefs").get_lakefs_client().repository_exists(name)
            for name in lakefs_repos
        ]
    )
    assert any(_prefix_keys(name) for name in lakefs_repos)
    assert all(_object_exists(storage_cleanup.lfs_key(sha)) for sha in lfs_shas)

    response = await admin_client.delete("/admin/api/users/owner", params={"force": "true"})

    assert response.status_code == 200
    body = response.json()
    assert body["storage_cleanup"] == "scheduled"
    assert sorted(body["deleted_repositories"]) == sorted(
        f"{repo.repo_type}:{repo.full_id}" for repo in repos
    )
    assert Repository.select().where(Repository.id.in_([r.id for r in repos])).count() == 0
    scheduled = {
        json.loads(row.payload)["lakefs_repo"]
        for row in BackgroundTask.select().where(BackgroundTask.kind == storage_cleanup.PURGE_KIND)
    }
    assert scheduled == set(lakefs_repos)

    statuses = await _drain_storage_tasks()

    assert set(statuses.values()) == {tasks.SUCCEEDED}
    for name in lakefs_repos:
        assert await _gone(name), f"LakeFS repository {name} still exists"
        assert _prefix_keys(name) == []
    assert not any(_object_exists(storage_cleanup.lfs_key(sha)) for sha in lfs_shas)
    assert LfsGcCandidate.select().count() == 0


async def test_deleting_a_user_without_repositories_schedules_nothing(admin_client):
    response = await admin_client.delete("/admin/api/users/outsider", params={"force": "true"})

    assert response.status_code == 200
    assert response.json()["storage_cleanup"] == "none"
    assert BackgroundTask.select().count() == 0


async def test_deleting_an_organization_schedules_its_repositories():
    org = User.get((User.username == "acme-labs") & (User.is_org == True))
    # Repository.owner is the creating user, even in an organization's
    # namespace; make the organization own its dataset for this test.
    Repository.update(owner=org).where(Repository.namespace == "acme-labs").execute()
    lakefs_repos = {
        resolve_lakefs_repo(repo) for repo in Repository.select().where(Repository.owner == org)
    }
    assert lakefs_repos

    delete_organization(org)

    assert User.get_or_none(User.id == org.id) is None
    scheduled = {
        json.loads(row.payload)["lakefs_repo"]
        for row in BackgroundTask.select().where(BackgroundTask.kind == storage_cleanup.PURGE_KIND)
    }
    assert scheduled == lakefs_repos
    assert set((await _drain_storage_tasks()).values()) == {tasks.SUCCEEDED}
    for name in lakefs_repos:
        assert await _gone(name)


async def test_deleting_a_repository_row_schedules_its_cleanup():
    repo = Repository.get(Repository.full_id == "owner/demo-model")
    lakefs_repo = resolve_lakefs_repo(repo)

    delete_repository(repo)

    assert Repository.get_or_none(Repository.id == repo.id) is None
    assert set((await _drain_storage_tasks()).values()) == {tasks.SUCCEEDED}
    assert await _gone(lakefs_repo)
    assert _prefix_keys(lakefs_repo) == []
