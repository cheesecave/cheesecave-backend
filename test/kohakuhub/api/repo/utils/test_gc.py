"""Tests for repository garbage-collection helpers, on real history and tombstone rows."""

from __future__ import annotations

import pytest

import kohakuhub.api.repo.utils.gc as gc_utils
from kohakuhub.db import File, LFSObjectHistory, LfsObjectTombstone
from test.kohakuhub.support.factories import make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


def _file(repo, path, sha):
    return File.create(
        repository=repo,
        path_in_repo=path,
        sha256=sha,
        lfs=True,
        owner=repo.owner,
        size=1,
    )


def test_track_lfs_object_records_history_linked_to_the_file_row(monkeypatch):
    repo = make_repo(make_user("owner"), "repo")
    tracked = _file(repo, "weights/model.bin", "a" * 64)
    monkeypatch.setattr(gc_utils.cfg.s3, "bucket", "hub-storage")

    gc_utils.track_lfs_object("model", "owner", "repo", "weights/model.bin", "a" * 64, 123, "commit-1")

    row = LFSObjectHistory.get(LFSObjectHistory.commit_id == "commit-1")
    assert row.repository_id == repo.id
    assert row.path_in_repo == "weights/model.bin"
    assert row.size == 123
    assert row.file_id == tracked.id


def test_track_lfs_object_without_a_file_row_keeps_history_unlinked():
    repo = make_repo(make_user("owner"), "repo")

    gc_utils.track_lfs_object("model", "owner", "repo", "x", "b" * 64, 1, "commit-2")

    row = LFSObjectHistory.get(LFSObjectHistory.commit_id == "commit-2")
    assert row.file_id is None


def test_track_lfs_object_ignores_an_unknown_repository():
    gc_utils.track_lfs_object("model", "owner", "missing", "x", "c" * 64, 1, "commit-3")

    assert LFSObjectHistory.select().where(LFSObjectHistory.commit_id == "commit-3").count() == 0


@pytest.mark.asyncio
async def test_check_lfs_recoverability_is_true_for_a_commit_without_lfs_files():
    repo = make_repo(make_user("owner"), "repo")

    assert await gc_utils.check_lfs_recoverability(repo, "commit-empty") == (True, [])


@pytest.mark.asyncio
async def test_check_lfs_recoverability_reports_missing_objects_and_tombstones(monkeypatch):
    repo = make_repo(make_user("owner"), "repo")
    present = "a" * 64
    absent = "b" * 64
    for path, sha in (("weights.bin", present), ("config.json", absent)):
        LFSObjectHistory.create(
            repository=repo, path_in_repo=path, sha256=sha, size=1, commit_id="commit-2"
        )

    async def exists_only_present(bucket, key):
        # S3 is external: it answers for the bucket, the rows decide the rest.
        return key.endswith(present)

    monkeypatch.setattr(gc_utils, "object_exists", exists_only_present)
    monkeypatch.setattr(gc_utils.cfg.s3, "bucket", "hub-storage")

    recoverable, missing_files = await gc_utils.check_lfs_recoverability(repo, "commit-2")
    assert recoverable is False
    assert missing_files == ["config.json"]

    # A tombstone says the content is gone even while storage still answers.
    LfsObjectTombstone.create(sha256=present, state="deleted")

    async def always_exists(bucket, key):
        return True

    monkeypatch.setattr(gc_utils, "object_exists", always_exists)
    recoverable, missing_files = await gc_utils.check_lfs_recoverability(repo, "commit-2")
    assert recoverable is False
    assert missing_files == ["weights.bin"]
