"""Tests for repository garbage-collection helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kohakuhub.api.repo.utils.gc as gc_utils


class _Expr:
    def __init__(self, value):
        self.value = value

    def __and__(self, other):
        return _Expr(("and", self.value, getattr(other, "value", other)))

    def __repr__(self):
        return repr(self.value)


class _Field:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return _Expr((self.name, "==", other))

    def desc(self):
        return _Expr((self.name, "desc"))

    def in_(self, values):
        return _Expr((self.name, "in", tuple(values)))

    def not_in(self, values):
        return _Expr((self.name, "not_in", tuple(values)))

    def __hash__(self):
        return hash(self.name)


class _Query:
    def __init__(self, items=None, count_result=None, execute_result=0):
        self.items = list(items or [])
        self.count_result = len(self.items) if count_result is None else count_result
        self.execute_result = execute_result
        self.where_calls = []
        self.order_by_calls = []
        self.select_calls = []

    def where(self, *args):
        self.where_calls.append(args)
        return self

    def order_by(self, *args):
        self.order_by_calls.append(args)
        return self

    def count(self):
        return self.count_result

    def execute(self):
        return self.execute_result

    def distinct(self):
        return self

    def select(self, *args):
        self.select_calls.append(args)
        return self

    def __iter__(self):
        return iter(self.items)


class _InsertQuery:
    def __init__(self):
        self.on_conflict_calls = []
        self.execute_calls = 0

    def on_conflict(self, **kwargs):
        self.on_conflict_calls.append(kwargs)
        return self

    def execute(self):
        self.execute_calls += 1
        return 1


class _FakeFileModel:
    repository = _Field("repository")
    path_in_repo = _Field("path_in_repo")
    sha256 = _Field("sha256")
    lfs = _Field("lfs")
    is_deleted = _Field("is_deleted")
    updated_at = _Field("updated_at")
    size = _Field("size")
    owner = _Field("owner")

    select_query = _Query()
    delete_query = _Query()
    get_or_none_result = None
    get_or_none_side_effect = None
    insert_calls = []

    @classmethod
    def reset(cls):
        cls.select_query = _Query()
        cls.delete_query = _Query()
        cls.get_or_none_result = None
        cls.get_or_none_side_effect = None
        cls.insert_calls = []

    @classmethod
    def select(cls, *args):
        return cls.select_query

    @classmethod
    def get_or_none(cls, *args):
        if cls.get_or_none_side_effect is not None:
            return cls.get_or_none_side_effect(*args)
        return cls.get_or_none_result

    @classmethod
    def insert(cls, **kwargs):
        cls.insert_calls.append(kwargs)
        return _InsertQuery()

    @classmethod
    def delete(cls):
        return cls.delete_query


class _FakeHistoryModel:
    repository = _Field("repository")
    path_in_repo = _Field("path_in_repo")
    created_at = _Field("created_at")
    sha256 = _Field("sha256")
    commit_id = _Field("commit_id")

    select_query = _Query()
    delete_query = _Query()

    @classmethod
    def reset(cls):
        cls.select_query = _Query()
        cls.delete_query = _Query()

    @classmethod
    def select(cls, *args):
        return cls.select_query

    @classmethod
    def delete(cls):
        return cls.delete_query


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


@pytest.fixture(autouse=True)
def _reset_fake_models():
    _FakeFileModel.reset()
    _FakeHistoryModel.reset()


def test_track_lfs_object_covers_repo_lookup(monkeypatch):
    repo = SimpleNamespace(full_id="owner/repo")
    file_fk = SimpleNamespace(id=1)
    created = []

    monkeypatch.setattr(gc_utils, "get_repository", lambda *_args: repo)
    monkeypatch.setattr(gc_utils, "File", _FakeFileModel)
    monkeypatch.setattr(gc_utils, "create_lfs_history", lambda **kwargs: created.append(kwargs))
    _FakeFileModel.get_or_none_result = file_fk

    gc_utils.track_lfs_object(
        "model",
        "owner",
        "repo",
        "weights/model.bin",
        "a" * 64,
        123,
        "commit-1",
    )

    assert created == [
        {
            "repository": repo,
            "path_in_repo": "weights/model.bin",
            "sha256": "a" * 64,
            "size": 123,
            "commit_id": "commit-1",
            "file": file_fk,
        }
    ]

    monkeypatch.setattr(gc_utils, "get_repository", lambda *_args: None)
    gc_utils.track_lfs_object("model", "owner", "repo", "x", "b" * 64, 1, "commit-2")
    assert len(created) == 1


@pytest.mark.asyncio
async def test_check_lfs_recoverability_covers_empty_and_missing_objects(monkeypatch):
    empty_repo = SimpleNamespace(lfs_history=SimpleNamespace(select=lambda: _Query(items=[])))
    monkeypatch.setattr(gc_utils, "LFSObjectHistory", _FakeHistoryModel)

    assert await gc_utils.check_lfs_recoverability(empty_repo, "commit-1") == (True, [])

    lfs_objects = [
        SimpleNamespace(path_in_repo="weights.bin", sha256="a" * 64),
        SimpleNamespace(path_in_repo="config.json", sha256="b" * 64),
    ]
    repo = SimpleNamespace(lfs_history=SimpleNamespace(select=lambda: _Query(items=lfs_objects)))
    monkeypatch.setattr(
        gc_utils,
        "object_exists",
        lambda bucket, key: _async_return(not key.endswith("b" * 64))(),
    )
    monkeypatch.setattr(gc_utils.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(gc_utils, "deleted_shas", lambda shas: set())

    recoverable, missing_files = await gc_utils.check_lfs_recoverability(repo, "commit-2")

    assert recoverable is False
    assert missing_files == ["config.json"]

    # History rows outlive a collected object; its tombstone decides, even
    # if storage still answers (a collection in progress).
    monkeypatch.setattr(gc_utils, "object_exists", lambda bucket, key: _async_return(True)())
    monkeypatch.setattr(gc_utils, "deleted_shas", lambda shas: {"a" * 64} & set(shas))

    recoverable, missing_files = await gc_utils.check_lfs_recoverability(repo, "commit-2")

    assert recoverable is False
    assert missing_files == ["weights.bin"]


@pytest.mark.asyncio
async def test_cleanup_repository_storage_deletes_only_the_repository_prefix(monkeypatch):
    prefixes = []

    async def fake_delete(bucket, prefix):
        prefixes.append(prefix)
        return 3

    monkeypatch.setattr(gc_utils, "delete_objects_with_prefix", fake_delete)

    result = await gc_utils.cleanup_repository_storage("model", "owner", "repo", "lakefs-repo")

    # Shared LFS objects are left to the background collection.
    assert result == {"repo_objects_deleted": 3}
    assert prefixes == ["lakefs-repo/"]
