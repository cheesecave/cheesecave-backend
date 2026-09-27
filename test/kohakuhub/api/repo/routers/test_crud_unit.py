"""Unit tests for repository CRUD routes and helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.api.repo.routers.crud as repo_crud
import kohakuhub.api.operation_capabilities as operation_capabilities
from kohakuhub.utils.lakefs import lakefs_repo_name


class _Expr:
    def __init__(self, value):
        self.value = value

    def __and__(self, other):
        return _Expr(("and", self.value, getattr(other, "value", other)))


class _Field:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return _Expr((self.name, "==", other))

    def __hash__(self):
        return hash(self.name)


class _Query:
    def __init__(self, items=None, execute_result=1):
        self.items = list(items or [])
        self.execute_result = execute_result
        self.where_calls = []

    def where(self, *args):
        self.where_calls.append(args)
        return self

    def execute(self):
        return self.execute_result

    def __iter__(self):
        return iter(self.items)


class _AtomicContext:
    def __init__(self, seen: dict):
        self.seen = seen

    def __enter__(self):
        self.seen["entered"] = self.seen.get("entered", 0) + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.seen["exited"] = self.seen.get("exited", 0) + 1
        return False


class _FakeRepositoryModel:
    repo_type = _Field("repo_type")
    namespace = _Field("namespace")
    name = _Field("name")
    id = _Field("id")

    select_query = _Query()
    update_query = _Query()
    get_or_create_calls = []

    @classmethod
    def reset(cls):
        cls.select_query = _Query()
        cls.update_query = _Query()
        cls.get_or_create_calls = []

    @classmethod
    def select(cls):
        return cls.select_query

    @classmethod
    def update(cls, **kwargs):
        cls.update_kwargs = kwargs
        return cls.update_query

    @classmethod
    def get_or_create(cls, **kwargs):
        cls.get_or_create_calls.append(kwargs)
        return SimpleNamespace(full_id=kwargs["full_id"]), True


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.raise_on = {}
        self.list_payloads = []
        self.repository_exists_values = []

    def _maybe_raise(self, name):
        error = self.raise_on.get(name)
        if error:
            raise error

    async def create_repository(self, **kwargs):
        self.calls.append(("create_repository", kwargs))
        self._maybe_raise("create_repository")
        return {"ok": True}

    async def delete_repository(self, **kwargs):
        self.calls.append(("delete_repository", kwargs))
        self._maybe_raise("delete_repository")
        return {"ok": True}

    async def list_objects(self, **kwargs):
        self.calls.append(("list_objects", kwargs))
        self._maybe_raise("list_objects")
        return self.list_payloads.pop(0)

    async def link_physical_address(self, **kwargs):
        self.calls.append(("link_physical_address", kwargs))
        self._maybe_raise("link_physical_address")
        return {"ok": True}

    async def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        self._maybe_raise("get_object")
        return b"content"

    async def upload_object(self, **kwargs):
        self.calls.append(("upload_object", kwargs))
        self._maybe_raise("upload_object")
        return {"ok": True}

    async def commit(self, **kwargs):
        self.calls.append(("commit", kwargs))
        self._maybe_raise("commit")
        return {"ok": True}

    async def repository_exists(self, repo_name):
        self.calls.append(("repository_exists", repo_name))
        if self.repository_exists_values:
            return self.repository_exists_values.pop(0)
        return False


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()


@pytest.fixture(autouse=True)
def _reset_repo_model():
    _FakeRepositoryModel.reset()


@pytest.mark.asyncio
async def test_create_repo_covers_conflicts_lakefs_failure_and_success(monkeypatch):
    user = SimpleNamespace(username="owner")
    client = _FakeClient()
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(
        repo_crud,
        "normalize_name",
        lambda name: name.lower().replace("-", "").replace("_", ""),
    )

    _FakeRepositoryModel.select_query = _Query(items=[SimpleNamespace(name="demo-model")])
    conflict = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="Demo_Model"), user=user
    )
    # huggingface_hub's create_repo(exist_ok=True) only shortcuts on 409, so the
    # conflict response now uses 409 Conflict with a JSON body carrying `url`.
    assert conflict.status_code == 409
    assert conflict.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert json.loads(bytes(conflict.body)).get("url")

    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: SimpleNamespace())
    exact_conflict = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )
    assert exact_conflict.status_code == 409
    assert json.loads(bytes(exact_conflict.body)).get("url")

    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    _FakeRepositoryModel.select_query = _Query(items=[])
    client.raise_on["create_repository"] = RuntimeError("lakefs failed")
    lakefs_error = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )
    assert lakefs_error.status_code == 500

    client.raise_on.pop("create_repository", None)
    success = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )
    assert success["repo_id"] == "owner/demo-model"
    assert _FakeRepositoryModel.get_or_create_calls


@pytest.mark.asyncio
async def test_delete_repo_covers_admin_validation_not_found_and_failures(monkeypatch):
    repo_row = SimpleNamespace(
        delete_instance=lambda: None,
        repo_type="model",
        full_id="owner/demo-model",
        lakefs_repo="model:owner/demo-model",
    )
    client = _FakeClient()
    atomic_state = {}

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "resolve_lakefs_repo", lambda repo: f"{repo.repo_type}:{repo.full_id}")
    monkeypatch.setattr(repo_crud, "check_repo_delete_permission", lambda repo, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "cleanup_repository_storage", lambda **kwargs: _async_return({"repo_objects_deleted": 1}))
    recorded = []
    monkeypatch.setattr(repo_crud, "record_repository_lfs", recorded.append)
    monkeypatch.setattr(repo_crud, "is_lakefs_not_found_error", lambda error: "404" in str(error))
    monkeypatch.setattr(repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext(atomic_state)))

    with pytest.raises(HTTPException) as admin_missing_org:
        await repo_crud.delete_repo(
            repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
            auth=(None, True),
        )
    assert admin_missing_org.value.status_code == 400

    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    not_found = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert not_found.status_code == 404

    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: repo_row)
    success = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert "deleted" in success["message"].lower()
    assert atomic_state == {"entered": 1, "exited": 1}
    # The repository's LFS objects become collection candidates with the row
    assert recorded == [repo_row]

    client.raise_on["delete_repository"] = RuntimeError("boom")
    failure = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert failure.status_code == 500

    client.raise_on["delete_repository"] = RuntimeError("404 missing")
    db_failure_repo = SimpleNamespace(
        delete_instance=lambda: (_ for _ in ()).throw(RuntimeError("db broke")),
        repo_type="model",
        full_id="owner/demo-model",
        lakefs_repo="model:owner/demo-model",
    )
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: db_failure_repo)
    db_failure = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert db_failure.status_code == 500


@pytest.mark.asyncio
async def test_migrate_lakefs_repository_covers_noop_missing_source_success_and_cleanup(monkeypatch):
    client = _FakeClient()
    from_repo = SimpleNamespace(full_id="owner/from")
    deleted_prefixes = []
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "get_repository", lambda repo_type, namespace, name: from_repo if name in {"from", "same"} else None)
    monkeypatch.setattr(repo_crud, "get_file", lambda repo, path: SimpleNamespace(lfs=path.endswith(".bin")) if path != "missing.txt" else None)
    monkeypatch.setattr(repo_crud, "should_use_lfs", lambda repo, path, size: path.endswith(".bin"))
    monkeypatch.setattr(repo_crud, "delete_objects_with_prefix", lambda bucket, prefix: deleted_prefixes.append((bucket, prefix)) or _async_return(2))
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")

    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("owner-to")
    )

    with pytest.raises(HTTPException) as missing_source:
        await repo_crud._migrate_lakefs_repository(
            "model",
            "owner/missing",
            "owner/to",
            from_lakefs_repo="owner-missing",
        )
    assert missing_source.value.status_code == 404

    client.list_payloads = [
        {
            "results": [
                {"path_type": "object", "path": "README.md", "size_bytes": 3, "checksum": "sha256:readme", "physical_address": "s3://bucket/repo/README.md"},
                {"path_type": "object", "path": "weights.bin", "size_bytes": 12, "checksum": "sha256:weights", "physical_address": "s3://bucket/lfs/weights"},
            ],
            "pagination": {"has_more": False},
        }
    ]
    created = await repo_crud._migrate_lakefs_repository(
        "model",
        "owner/from",
        "owner/to",
        from_lakefs_repo="owner-from",
    )
    assert created == "owner-to"
    method_names = [name for name, _kwargs in client.calls]
    assert "create_repository" in method_names
    assert "link_physical_address" in method_names
    assert "upload_object" in method_names
    assert deleted_prefixes[-1] == ("hub-storage", "owner-from/")

    client.raise_on["create_repository"] = RuntimeError("migration broke")
    with pytest.raises(HTTPException) as migration_error:
        await repo_crud._migrate_lakefs_repository(
            "model",
            "owner/from",
            "owner/to",
            from_lakefs_repo="owner-from",
        )
    assert migration_error.value.status_code == 500


@pytest.mark.asyncio
async def test_migrate_lakefs_repository_aborts_without_deleting_source_on_object_failure(monkeypatch):
    """One object failing to migrate must abort the migration (#107).

    Skipping it used to commit the partial copy and then delete the source
    LakeFS repository and S3 prefix, losing the object for good.
    """
    client = _FakeClient()
    from_repo = SimpleNamespace(full_id="owner/from")
    deleted_prefixes = []
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "get_repository", lambda repo_type, namespace, name: from_repo)
    monkeypatch.setattr(repo_crud, "get_file", lambda repo, path: SimpleNamespace(lfs=False))
    monkeypatch.setattr(repo_crud, "delete_objects_with_prefix", lambda bucket, prefix: deleted_prefixes.append(prefix) or _async_return(0))
    client.list_payloads = [
        {
            "results": [
                {"path_type": "object", "path": "README.md", "size_bytes": 3, "checksum": "c1", "physical_address": "s3://b/r/README.md"},
                {"path_type": "object", "path": "config.json", "size_bytes": 4, "checksum": "c2", "physical_address": "s3://b/r/config.json"},
            ],
            "pagination": {"has_more": False},
        }
    ]
    client.raise_on["upload_object"] = RuntimeError("s3 hiccup")
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("owner-to")
    )

    with pytest.raises(HTTPException) as failure:
        await repo_crud._migrate_lakefs_repository(
            "model",
            "owner/from",
            "owner/to",
            from_lakefs_repo="owner-from",
        )

    assert failure.value.status_code == 500
    assert "README.md" in failure.value.detail["error"]
    calls = [(name, kwargs.get("repository")) for name, kwargs in client.calls]
    assert ("commit", "owner-to") not in calls
    assert ("delete_repository", "owner-from") not in calls
    assert ("delete_repository", "owner-to") in calls  # the half-built target is removed
    assert deleted_prefixes == []


@pytest.mark.asyncio
async def test_migrate_lakefs_repository_fails_cleanly_without_a_usable_target_id(monkeypatch):
    """If no LakeFS id can be created for the target, the migration stops before
    touching anything: no target to clean up, and the source stays intact."""
    client = _FakeClient()
    client.list_payloads = [{"results": [], "pagination": {"has_more": False}}]
    client.raise_on["create_repository"] = RuntimeError(
        'LakeFS API error 409: {"message":"error creating repository: not unique"}'
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_a: SimpleNamespace(full_id="owner/from"))
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("owner-to"))

    with pytest.raises(HTTPException) as failure:
        await repo_crud._migrate_lakefs_repository(
            "model", "owner/from", "owner/to", from_lakefs_repo="owner-from"
        )

    assert failure.value.status_code == 500
    assert "No usable LakeFS repository id" in failure.value.detail["error"]
    assert not [call for call in client.calls if call[0] == "delete_repository"]


@pytest.mark.asyncio
async def test_migrate_lakefs_repository_failure_survives_target_cleanup_errors(monkeypatch):
    """Removing the half-built target is best effort: its own failure must not
    replace the migration error the caller needs to see."""
    client = _FakeClient()
    client.list_payloads = [
        {
            "results": [{"path_type": "object", "path": "a.txt", "size_bytes": 1, "checksum": "c", "physical_address": "p"}],
            "pagination": {"has_more": False},
        }
    ]
    client.raise_on["upload_object"] = RuntimeError("s3 hiccup")
    client.raise_on["delete_repository"] = RuntimeError("lakefs down")
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_a: SimpleNamespace(full_id="owner/from"))
    monkeypatch.setattr(repo_crud, "get_file", lambda repo, path: SimpleNamespace(lfs=False))
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("owner-to"))

    with pytest.raises(HTTPException) as failure:
        await repo_crud._migrate_lakefs_repository(
            "model", "owner/from", "owner/to", from_lakefs_repo="owner-from"
        )

    assert "failed to migrate" in failure.value.detail["error"]


def test_update_repository_database_records_covers_same_and_cross_namespace_moves(monkeypatch):
    increments = []
    repo_row = SimpleNamespace(id=1, quota_bytes=100, used_bytes=50, private=False)
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(repo_crud, "get_organization", lambda namespace: SimpleNamespace() if namespace.startswith("org") else None)
    monkeypatch.setattr(repo_crud, "increment_storage", lambda **kwargs: increments.append(kwargs))

    repo_crud._update_repository_database_records(
        repo_row=repo_row,
        from_id="owner/from",
        to_id="owner/to",
        from_namespace="owner",
        to_namespace="owner",
        to_name="to",
        moving_namespace=False,
        repo_size=0,
        to_lakefs_repo="m-owner-to",
    )
    assert _FakeRepositoryModel.update_kwargs["quota_bytes"] == 100
    assert _FakeRepositoryModel.update_kwargs["lakefs_repo"] == "m-owner-to", (
        "the row must point at the LakeFS repository the migration created"
    )

    repo_crud._update_repository_database_records(
        repo_row=repo_row,
        from_id="owner/from",
        to_id="org-team/to",
        from_namespace="owner",
        to_namespace="org-team",
        to_name="to",
        moving_namespace=True,
        repo_size=25,
        to_lakefs_repo="m-org-team-to",
    )
    assert _FakeRepositoryModel.update_kwargs["quota_bytes"] is None
    assert increments[0]["bytes_delta"] == -25
    assert increments[1]["bytes_delta"] == 25


@pytest.mark.asyncio
async def test_move_repo_covers_validation_quota_and_metadata_only_success(monkeypatch):
    """A move renames the KHub row only; LakeFS and S3 are never touched (#107).

    Recreating the LakeFS repository used to keep only the main head, dropping
    history, other branches and tags. Since migration 016 the LakeFS id lives on
    the row, so the row can keep it across a rename.
    """
    repo_row = SimpleNamespace(
        private=False,
        repo_type="model",
        full_id="owner/from",
        lakefs_repo="m-owner-from",
    )
    atomic_state = {}

    def _forbidden(name):
        def _raise(*_args, **_kwargs):
            raise AssertionError(f"move must not call {name}")

        return _raise

    monkeypatch.setattr(repo_crud, "check_repo_delete_permission", lambda repo, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "get_repository", lambda repo_type, namespace, name: repo_row if (namespace, name) == ("owner", "from") else None)
    monkeypatch.setattr(repo_crud, "calculate_repository_storage", lambda repo: _async_return({"total_bytes": 12}))
    monkeypatch.setattr(repo_crud, "get_organization", lambda namespace: None)
    monkeypatch.setattr(repo_crud, "check_quota", lambda **kwargs: (True, None))
    monkeypatch.setattr(repo_crud, "_update_repository_database_records", lambda **kwargs: atomic_state.setdefault("updated", []).append(kwargs))
    monkeypatch.setattr(repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext(atomic_state)))
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    _FakeRepositoryModel.reset()
    for name in (
        "_migrate_lakefs_repository",
        "allocate_lakefs_repo_name",
        "cleanup_repository_storage",
        "get_lakefs_client",
    ):
        monkeypatch.setattr(repo_crud, name, _forbidden(name))

    bad_source = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="bad", toRepo="owner/to", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert bad_source.status_code == 400

    bad_target = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="bad", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert bad_target.status_code == 400

    monkeypatch.setattr(repo_crud, "get_repository", lambda repo_type, namespace, name: None)
    not_found = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/to", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert not_found.status_code == 404

    monkeypatch.setattr(repo_crud, "get_repository", lambda repo_type, namespace, name: repo_row if (namespace, name) == ("owner", "from") else SimpleNamespace())
    exists = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/to", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert exists.status_code == 409
    assert exists.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS

    monkeypatch.setattr(repo_crud, "get_repository", lambda repo_type, namespace, name: repo_row if (namespace, name) == ("owner", "from") else None)
    monkeypatch.setattr(repo_crud, "check_quota", lambda **kwargs: (False, "quota exceeded"))
    with pytest.raises(HTTPException) as quota_error:
        await repo_crud.move_repo(
            repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="other/to", type="model"),
            auth=(SimpleNamespace(username="owner"), False),
        )
    assert quota_error.value.status_code == 400

    monkeypatch.setattr(repo_crud, "check_quota", lambda **kwargs: (True, None))
    success = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="other/to", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert success["success"] is True
    assert success["url"] == "https://hub.example.com/models/other/to"
    update = atomic_state["updated"][-1]
    # The row keeps pointing at the LakeFS repository that holds its data.
    assert update["to_lakefs_repo"] == "m-owner-from"
    assert (update["to_namespace"], update["to_name"], update["moving_namespace"]) == ("other", "to", True)


@pytest.mark.asyncio
async def test_squash_repo_covers_validation_success_and_recovery(monkeypatch):
    monkeypatch.setattr(
        operation_capabilities.cfg.app, "repository_squash_enabled", True
    )
    repo_row = SimpleNamespace(
        private=False,
        repo_type="model",
        full_id="owner/demo-model",
        lakefs_repo="model:owner/demo-model",
    )
    # Step 2 reloads the row under the temporary name and resolves its LakeFS id
    # from it, so the temp row carries its own identity too.
    temp_repo = SimpleNamespace(
        private=False,
        repo_type="model",
        full_id="owner/demo-squash-abc12345",
        lakefs_repo="model:owner/demo-squash-abc12345",
    )
    atomic_state = {}
    client = _FakeClient()
    client.repository_exists_values = [True, False, True, False]
    repo_lookup = {"initial": repo_row, "temp": temp_repo, "final": repo_row}

    def fake_get_repository(repo_type, namespace, name):
        if name == "demo":
            return repo_lookup["initial"]
        if name.startswith("demo-squash-"):
            return repo_lookup["temp"]
        return repo_lookup["final"]

    monkeypatch.setattr(repo_crud, "get_repository", fake_get_repository)
    monkeypatch.setattr(repo_crud, "check_repo_delete_permission", lambda repo, user, is_admin=False: None)
    # The migration allocates and returns the LakeFS id it created.
    monkeypatch.setattr(repo_crud, "_migrate_lakefs_repository", lambda **kwargs: _async_return(f"lakefs:{kwargs['to_id']}"))
    monkeypatch.setattr(repo_crud, "_update_repository_database_records", lambda **kwargs: atomic_state.setdefault("updated", []).append(kwargs))
    monkeypatch.setattr(repo_crud, "cleanup_repository_storage", lambda **kwargs: _async_return({"repo_objects_deleted": 1, "lfs_objects_deleted": 0, "lfs_history_deleted": 0}))
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "resolve_lakefs_repo", lambda repo: f"{repo.repo_type}:{repo.full_id}")
    monkeypatch.setattr(repo_crud, "update_repository_storage", lambda repo: _async_return(None))
    monkeypatch.setattr(repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext(atomic_state)))
    monkeypatch.setattr(repo_crud.uuid, "uuid4", lambda: SimpleNamespace(hex="abc12345deadbeef"))

    bad_id = await repo_crud.squash_repo(
        repo_crud.SquashRepoPayload(repo="bad", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert bad_id.status_code == 400

    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    not_found = await repo_crud.squash_repo(
        repo_crud.SquashRepoPayload(repo="owner/demo", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert not_found.status_code == 404

    monkeypatch.setattr(repo_crud, "get_repository", fake_get_repository)
    success = await repo_crud.squash_repo(
        repo_crud.SquashRepoPayload(repo="owner/demo", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )
    assert success["success"] is True
    # Each hop points the row at the LakeFS repository its migration created.
    assert [u["to_lakefs_repo"] for u in atomic_state["updated"][-2:]] == [
        f"lakefs:{atomic_state['updated'][-2]['to_id']}",
        f"lakefs:{atomic_state['updated'][-1]['to_id']}",
    ]
    assert atomic_state["updated"][-1]["to_id"] == atomic_state["updated"][-2]["from_id"]

    monkeypatch.setattr(repo_crud, "_migrate_lakefs_repository", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("squash failed")))
    with pytest.raises(HTTPException) as squash_error:
        await repo_crud.squash_repo(
            repo_crud.SquashRepoPayload(repo="owner/demo", type="model"),
            auth=(SimpleNamespace(username="owner"), False),
        )
    assert squash_error.value.status_code == 500


@pytest.mark.asyncio
async def test_disabled_squash_rejects_before_repository_lookup(monkeypatch):
    monkeypatch.setattr(
        operation_capabilities.cfg.app, "repository_squash_enabled", False
    )

    def unexpected_repository_lookup(*_args):
        raise AssertionError("disabled operation reached repository lookup")

    monkeypatch.setattr(repo_crud, "get_repository", unexpected_repository_lookup)

    with pytest.raises(HTTPException) as error:
        await repo_crud.squash_repo(
            repo_crud.SquashRepoPayload(repo="owner/demo", type="model"),
            auth=(SimpleNamespace(username="owner"), False),
        )

    assert error.value.status_code == 503
    assert error.value.detail["code"] == "operation_disabled"
    assert error.value.detail["operation"] == "squash"


# ---------------------------------------------------------------------------
# Regression tests for the 302->403 orphan-state class of bugs.
#
# Two failure modes were observed in production and are now covered:
#
#   (A) `create_repo` could fail forever on a stale `_lakefs/dummy` marker left
#       behind by a previously-aborted creation. LakeFS would refuse the new
#       create with "storage namespace already in use" pointing at our own
#       repo namespace, and the user could never recreate the repo.
#
#   (B) `delete_repo` wiped S3 storage *before* deleting LakeFS metadata. If
#       the LakeFS deletion then failed (non-404), the LakeFS repo survived but
#       its underlying S3 objects were gone — subsequent reads issued a 302
#       redirect that resolved to a 403 on the now-empty S3 prefix.
#
# Each test below was chosen so that it FAILS on dev/narugo1992 (pre-fix) and
# PASSES on bugfix/302-to-403 (post-fix). The pure-helper tests double as
# guardrail documentation for the new safety checks.
# ---------------------------------------------------------------------------


def test_is_lakefs_namespace_in_use_error_only_matches_exact_dummy_marker():
    """The heal path must only fire for the precise namespace-in-use error.

    A broader matcher would risk wiping unrelated user data on the next retry,
    so this guardrail is intentionally narrow: lowered text contains the LakeFS
    phrase, the error references *our* storage namespace verbatim, and it names
    `_lakefs/dummy` (the only object LakeFS itself plants when initialising a
    new namespace).
    """
    storage = "s3://hub-storage/model:owner/demo-model"

    matching = RuntimeError(
        f"Storage namespace already in use: namespace={storage}, key=_lakefs/dummy"
    )
    assert repo_crud._is_lakefs_namespace_in_use_error(matching, storage) is True

    # Different storage namespace -> not our problem to heal.
    foreign = RuntimeError(
        "Storage namespace already in use: namespace=s3://other/foo, key=_lakefs/dummy"
    )
    assert repo_crud._is_lakefs_namespace_in_use_error(foreign, storage) is False

    # Right namespace but no dummy marker mention -> wrong error shape.
    no_marker = RuntimeError(f"Storage namespace already in use: namespace={storage}")
    assert repo_crud._is_lakefs_namespace_in_use_error(no_marker, storage) is False

    # Completely unrelated error.
    unrelated = RuntimeError("permission denied")
    assert repo_crud._is_lakefs_namespace_in_use_error(unrelated, storage) is False


def test_has_only_internal_lakefs_markers_refuses_to_clean_user_data():
    """Cleanup must only proceed when every sampled key is an internal marker.

    The presence of even one user object is enough to abort — the namespace is
    not orphaned, we just hit a transient LakeFS state, and blasting it would
    destroy real data.
    """
    prefix = "model:owner/demo-model/"

    only_internal = ["model:owner/demo-model/_lakefs/dummy"]
    assert repo_crud._has_only_internal_lakefs_markers(only_internal, prefix) is True

    with_user_object = [
        "model:owner/demo-model/_lakefs/dummy",
        "model:owner/demo-model/data/train.bin",
    ]
    assert (
        repo_crud._has_only_internal_lakefs_markers(with_user_object, prefix) is False
    )

    foreign_prefix = ["other:owner/demo-model/_lakefs/dummy"]
    assert (
        repo_crud._has_only_internal_lakefs_markers(foreign_prefix, prefix) is False
    )

    # Empty sample defaults to "not safe" unless explicitly opted in. The
    # opt-in is what `create_repo` uses when LakeFS itself reports the
    # internal-marker conflict but S3 lists nothing visible.
    assert repo_crud._has_only_internal_lakefs_markers([], prefix) is False
    assert (
        repo_crud._has_only_internal_lakefs_markers([], prefix, allow_empty=True)
        is True
    )


@pytest.mark.asyncio
async def test_create_repo_heals_orphan_dummy_marker_and_retries_lakefs_create(
    monkeypatch,
):
    """Reproduces (A): pre-fix, the first LakeFS error short-circuited to 500.

    Post-fix, the orphan dummy marker is recognised, removed via the safe
    cleanup path, and the LakeFS create is retried exactly once.
    """
    user = SimpleNamespace(username="owner")

    storage_namespace = "s3://hub-storage/model:owner/demo-model"
    namespace_in_use_error = RuntimeError(
        "Storage namespace already in use: "
        f"namespace={storage_namespace}, key=_lakefs/dummy"
    )

    create_attempts = {"count": 0}

    class _HealFlowClient(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            create_attempts["count"] += 1
            if create_attempts["count"] == 1:
                raise namespace_in_use_error
            return {"ok": True}

    client = _HealFlowClient()
    # `_cleanup_orphan_namespace_if_safe` first verifies LakeFS truly has no
    # repo of that name before touching anything in S3.
    client.repository_exists_values = [False]

    delete_marker_calls = []

    async def fake_list_keys(repo_prefix, max_keys=20):
        # Simulate LakeFS having reported an internal-marker conflict while S3
        # lists nothing visible — the `allow_empty_internal_marker` branch.
        return []

    async def fake_delete_marker(repo_prefix):
        delete_marker_calls.append(repo_prefix)
        return True

    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(
        repo_crud,
        "check_namespace_permission",
        lambda namespace, user, is_admin=False: None,
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    # create_repo allocates its LakeFS id rather than deriving it, so the
    # allocator is the seam that fixes the name this test asserts against.
    monkeypatch.setattr(
        repo_crud,
        "allocate_lakefs_repo_name",
        lambda client, repo_type, repo_id, **_kwargs: _async_return(f"{repo_type}:{repo_id}"),
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())

    # The heal helpers exist only on the fix branch; `raising=False` keeps the
    # patch call safe so the failure mode on a pre-fix branch is the assertion
    # below (server error response), not an AttributeError on monkeypatch.
    monkeypatch.setattr(
        repo_crud, "_list_repo_namespace_keys", fake_list_keys, raising=False
    )
    monkeypatch.setattr(
        repo_crud,
        "_delete_exact_repo_dummy_marker",
        fake_delete_marker,
        raising=False,
    )

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )

    # Pre-fix: the first LakeFS error returned a 500 Response. Indexing into a
    # Response with ["repo_id"] would raise — failing this assertion.
    assert isinstance(response, dict), (
        "create_repo should heal the orphan dummy marker and return a success "
        f"payload; got {response!r}"
    )
    assert response["repo_id"] == "owner/demo-model"
    assert create_attempts["count"] == 2, (
        "LakeFS create_repository must be retried exactly once after a "
        "successful orphan cleanup"
    )
    assert delete_marker_calls == ["model:owner/demo-model/"], (
        "The cleanup must target only this repo's exact prefix, not a broader "
        f"path; got {delete_marker_calls!r}"
    )
    assert _FakeRepositoryModel.get_or_create_calls, (
        "After a successful retry, the DB row must still be persisted"
    )


@pytest.mark.asyncio
async def test_create_repo_does_not_retry_on_unrelated_lakefs_error(monkeypatch):
    """The heal path must stay narrow: any error other than the exact namespace
    conflict bubbles up as 500 with no retry, no S3 listing, no marker delete.
    """
    user = SimpleNamespace(username="owner")
    create_attempts = {"count": 0}

    class _FailingClient(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            create_attempts["count"] += 1
            raise RuntimeError("lakefs is on fire")

    client = _FailingClient()

    list_calls = []
    delete_calls = []

    async def fake_list_keys(repo_prefix, max_keys=20):
        list_calls.append(repo_prefix)
        return []

    async def fake_delete_marker(repo_prefix):
        delete_calls.append(repo_prefix)
        return True

    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(
        repo_crud,
        "check_namespace_permission",
        lambda namespace, user, is_admin=False: None,
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud,
        "resolve_lakefs_repo",
        lambda repo: f"{repo.repo_type}:{repo.full_id}",
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())
    monkeypatch.setattr(
        repo_crud, "_list_repo_namespace_keys", fake_list_keys, raising=False
    )
    monkeypatch.setattr(
        repo_crud,
        "_delete_exact_repo_dummy_marker",
        fake_delete_marker,
        raising=False,
    )

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )

    assert getattr(response, "status_code", None) == 500
    assert create_attempts["count"] == 1, "Unrelated errors must not trigger a retry"
    assert list_calls == [], "S3 listing must not run for unrelated errors"
    assert delete_calls == [], "Marker delete must not run for unrelated errors"


@pytest.mark.asyncio
async def test_delete_repo_runs_lakefs_metadata_delete_before_s3_cleanup(monkeypatch):
    """Reproduces (B): delete order matters.

    Pre-fix, S3 cleanup was step 3 and LakeFS delete was step 4. If LakeFS
    deletion failed for any non-404 reason, the repo was orphaned: LakeFS
    metadata still pointed at an S3 prefix that had been wiped out, so reads
    issued a 302 redirect that resolved to a 403. The fix swaps the order so
    LakeFS metadata is removed first; S3 cleanup only follows once LakeFS no
    longer references the storage.
    """
    repo_row = SimpleNamespace(
        delete_instance=lambda: None,
        repo_type="model",
        full_id="owner/demo-model",
        lakefs_repo="model:owner/demo-model",
    )
    client = _FakeClient()
    atomic_state = {}
    call_order: list[str] = []

    original_delete = client.delete_repository

    async def tracked_lakefs_delete(**kwargs):
        call_order.append("lakefs_delete")
        return await original_delete(**kwargs)

    client.delete_repository = tracked_lakefs_delete

    async def fake_cleanup(**kwargs):
        call_order.append("s3_cleanup")
        return {
            "repo_objects_deleted": 1,
            "lfs_objects_deleted": 0,
            "lfs_history_deleted": 0,
        }

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud,
        "resolve_lakefs_repo",
        lambda repo: f"{repo.repo_type}:{repo.full_id}",
    )
    monkeypatch.setattr(
        repo_crud,
        "check_repo_delete_permission",
        lambda repo, user, is_admin=False: None,
    )
    monkeypatch.setattr(repo_crud, "cleanup_repository_storage", fake_cleanup)
    monkeypatch.setattr(repo_crud, "record_repository_lfs", lambda repo: 0)
    monkeypatch.setattr(
        repo_crud, "is_lakefs_not_found_error", lambda error: "404" in str(error)
    )
    monkeypatch.setattr(
        repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext(atomic_state))
    )
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: repo_row)

    success = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert "deleted" in success["message"].lower()
    assert call_order == ["lakefs_delete", "s3_cleanup"], (
        "Pre-fix order was [s3_cleanup, lakefs_delete] which produced 302->403 "
        "on partial failures. LakeFS metadata MUST be deleted before S3 "
        f"cleanup. Got {call_order!r}"
    )


@pytest.mark.asyncio
async def test_delete_repo_skips_s3_cleanup_when_lakefs_delete_fails(monkeypatch):
    """Reproduces (B) end-state: when LakeFS deletion fails (non-404), S3 must
    remain untouched. Pre-fix, S3 cleanup had already run before LakeFS was
    even tried, so the failed delete left an orphan LakeFS repo over wiped S3
    storage — the exact state that surfaced as 302->403 to clients.
    """
    repo_row = SimpleNamespace(
        delete_instance=lambda: None,
        repo_type="model",
        full_id="owner/demo-model",
        lakefs_repo="model:owner/demo-model",
    )
    client = _FakeClient()
    client.raise_on["delete_repository"] = RuntimeError("LakeFS internal error 500")
    atomic_state = {}
    cleanup_calls: list[dict] = []

    async def fake_cleanup(**kwargs):
        cleanup_calls.append(kwargs)
        return {
            "repo_objects_deleted": 1,
            "lfs_objects_deleted": 0,
            "lfs_history_deleted": 0,
        }

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud,
        "resolve_lakefs_repo",
        lambda repo: f"{repo.repo_type}:{repo.full_id}",
    )
    monkeypatch.setattr(
        repo_crud,
        "check_repo_delete_permission",
        lambda repo, user, is_admin=False: None,
    )
    monkeypatch.setattr(repo_crud, "cleanup_repository_storage", fake_cleanup)
    monkeypatch.setattr(repo_crud, "record_repository_lfs", lambda repo: 0)
    monkeypatch.setattr(
        repo_crud, "is_lakefs_not_found_error", lambda error: "404" in str(error)
    )
    monkeypatch.setattr(
        repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext(atomic_state))
    )
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: repo_row)

    response = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert getattr(response, "status_code", None) == 500
    assert cleanup_calls == [], (
        "Pre-fix bug: S3 cleanup ran first, so a later LakeFS delete failure "
        "left a live LakeFS repo pointing at wiped S3 storage. With the fix, "
        "S3 must NOT be touched when LakeFS delete fails. Got "
        f"{cleanup_calls!r}"
    )


@pytest.mark.asyncio
async def test_delete_repo_treats_lakefs_404_as_success_and_continues_to_s3(
    monkeypatch,
):
    """A LakeFS 404 (already deleted) must not block S3 cleanup. This guards
    the recovery path: after a previous delete crashed mid-way the user can
    safely retry and reach a clean end state.
    """
    repo_row = SimpleNamespace(
        delete_instance=lambda: None,
        repo_type="model",
        full_id="owner/demo-model",
        lakefs_repo="model:owner/demo-model",
    )
    client = _FakeClient()
    client.raise_on["delete_repository"] = RuntimeError("404 repository not found")
    atomic_state = {}
    cleanup_calls: list[dict] = []

    async def fake_cleanup(**kwargs):
        cleanup_calls.append(kwargs)
        return {
            "repo_objects_deleted": 0,
            "lfs_objects_deleted": 0,
            "lfs_history_deleted": 0,
        }

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud,
        "resolve_lakefs_repo",
        lambda repo: f"{repo.repo_type}:{repo.full_id}",
    )
    monkeypatch.setattr(
        repo_crud,
        "check_repo_delete_permission",
        lambda repo, user, is_admin=False: None,
    )
    monkeypatch.setattr(repo_crud, "cleanup_repository_storage", fake_cleanup)
    monkeypatch.setattr(repo_crud, "record_repository_lfs", lambda repo: 0)
    monkeypatch.setattr(
        repo_crud, "is_lakefs_not_found_error", lambda error: "404" in str(error)
    )
    monkeypatch.setattr(
        repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext(atomic_state))
    )
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: repo_row)

    success = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert "deleted" in success["message"].lower()
    assert len(cleanup_calls) == 1, (
        "After a 404 from LakeFS, S3 cleanup must still run so leftover "
        "objects from a prior partial delete can be reaped"
    )


# ---------------------------------------------------------------------------
# Issue #93: a renamed repo's freed name stays unusable while LakeFS finishes
# deleting the old repository asynchronously.
# ---------------------------------------------------------------------------


def test_is_lakefs_repo_id_taken_error_matches_only_the_async_deletion_conflict():
    taken = RuntimeError(
        "LakeFS API error 409 Conflict for POST http://lakefs:28000/api/v1/repositories: "
        '{"message":"error creating repository: not unique"}'
    )
    assert repo_crud._is_lakefs_repo_id_taken_error(taken) is True

    # The namespace-in-use error has its own (S3 marker cleanup) recovery path
    # and must not be reported as a retryable id conflict.
    namespace_in_use = RuntimeError(
        "LakeFS API error 400 Bad Request: failed to create repository: found lakeFS "
        "objects in the storage (:s3://hub-storage/d-owner-demo-x) key(_lakefs/dummy): "
        "storage namespace already in use"
    )
    assert repo_crud._is_lakefs_repo_id_taken_error(namespace_in_use) is False

    for unrelated in (
        RuntimeError("lakefs is on fire"),
        RuntimeError("LakeFS API error 404 Not Found"),
        RuntimeError('409 Conflict {"message":"something else entirely"}'),
        RuntimeError('{"message":"error creating repository: not unique"}'),  # no 409
    ):
        assert repo_crud._is_lakefs_repo_id_taken_error(unrelated) is False


def test_repo_recycling_response_matches_the_huggingface_hub_retry_contract():
    """`huggingface_hub.HfApi.create_repo` retries transparently, but only on a
    409 whose *body* contains this exact sentence. The check has been in place
    unchanged from 0.20.3 through 1.x, so the wording is load-bearing: without
    it, `create_repo(exist_ok=True)` instead swallows the 409 as "already
    exists" and the caller proceeds against a repo that does not exist.
    """
    response = repo_crud._repo_recycling_response("dataset", "owner/demo")

    assert response.status_code == 409
    body = bytes(response.body).decode()
    assert (
        "Cannot create repo: another conflicting operation is in progress" in body
    ), "hf_hub matches this substring against r.text, so it must be in the body"

    payload = json.loads(body)
    assert payload["repo_id"] == "owner/demo"
    # Header protocol clients still get a structured code, even though
    # hf_raise_for_status has no 409 branch that reads it.
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_NAME_RECYCLING


@pytest.mark.asyncio
async def test_create_repo_returns_retryable_conflict_when_no_lakefs_id_is_usable(
    monkeypatch,
):
    """Last resort only: a taken id is normally stepped over (see
    test_create_repo_steps_over_a_taken_lakefs_id). When every fresh id is taken
    too, the client gets huggingface_hub's retryable conflict, not an error."""
    user = SimpleNamespace(username="owner")
    sleeps = []

    class _IdTakenClient(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            raise RuntimeError(
                "LakeFS API error 409 Conflict for POST /repositories: "
                '{"message":"error creating repository: not unique"}'
            )

    client = _IdTakenClient()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(repo_crud.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(
        repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )

    assert response.status_code == 409
    assert "another conflicting operation is in progress" in bytes(response.body).decode()
    attempts = [name for name, _kwargs in client.calls if name == "create_repository"]
    assert len(attempts) == repo_crud.LAKEFS_CREATE_ATTEMPTS
    assert sleeps == [repo_crud.LAKEFS_RECYCLING_HOLD_SECONDS], (
        "hf_hub's retry loop has no backoff of its own, so the server supplies "
        "the delay by holding the response briefly"
    )
    assert not _FakeRepositoryModel.get_or_create_calls, (
        "A retryable conflict must not leave a half-created DB row behind"
    )


def _create_repo_env(monkeypatch, client, allocations):
    """Shared stubs for create_repo tests that drive the LakeFS id loop."""
    allocated = []

    async def fake_allocate(client_, repo_type, repo_id, exclude=frozenset(), **_kwargs):
        allocated.append(set(exclude))
        return next(name for name in allocations if name not in exclude)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(repo_crud.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", fake_allocate)
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())
    return allocated


@pytest.mark.asyncio
async def test_create_repo_steps_over_a_taken_lakefs_id(monkeypatch):
    """An id that LakeFS reports taken at create time (still being deleted, or a
    probe race) is skipped automatically: the user gets their repository under a
    fresh LakeFS id instead of an error or a "retry later"."""
    _FakeRepositoryModel.get_or_create_calls = []

    class _FirstIdTaken(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            if kwargs["name"] == "m-owner-demo-gen0":
                raise RuntimeError('LakeFS API error 409: {"message":"error creating repository: not unique"}')

    client = _FirstIdTaken()
    allocated = _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0", "m-owner-demo-gen1"])

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response["repo_id"] == "owner/demo-model"
    assert allocated == [set(), {"m-owner-demo-gen0"}]
    created = [kwargs["name"] for name, kwargs in client.calls if name == "create_repository"]
    assert created == ["m-owner-demo-gen0", "m-owner-demo-gen1"]
    assert created_storage_namespace(client) == "s3://hub-storage/m-owner-demo-gen1"
    assert _FakeRepositoryModel.get_or_create_calls[-1]["defaults"]["lakefs_repo"] == "m-owner-demo-gen1"


def created_storage_namespace(client):
    return [kwargs for name, kwargs in client.calls if name == "create_repository"][-1][
        "storage_namespace"
    ]


@pytest.mark.asyncio
async def test_create_repo_steps_over_a_storage_namespace_it_cannot_heal(monkeypatch):
    """A leftover storage namespace that is not safe to delete used to fail the
    create with a 500; now the create moves on to a fresh LakeFS id."""
    _FakeRepositoryModel.get_or_create_calls = []

    class _NamespaceInUse(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            if kwargs["name"] == "m-owner-demo-gen0":
                raise RuntimeError(
                    "LakeFS API error 400: storage namespace already in use: "
                    f"{kwargs['storage_namespace']}/dummy"
                )

    client = _NamespaceInUse()
    allocated = _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0", "m-owner-demo-gen1"])
    monkeypatch.setattr(repo_crud, "_is_lakefs_namespace_in_use_error", lambda error, namespace: "already in use" in str(error))
    monkeypatch.setattr(repo_crud, "_cleanup_orphan_namespace_if_safe", lambda *a, **k: _async_return(False))

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response["repo_id"] == "owner/demo-model"
    assert allocated == [set(), {"m-owner-demo-gen0"}]
    assert _FakeRepositoryModel.get_or_create_calls[-1]["defaults"]["lakefs_repo"] == "m-owner-demo-gen1"


@pytest.mark.asyncio
async def test_create_repo_persists_the_allocated_lakefs_repo_id(monkeypatch):
    user = SimpleNamespace(username="owner")
    client = _FakeClient()

    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(
        repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo-gen1")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())

    result = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )

    assert result["repo_id"] == "owner/demo-model"
    created_kwargs = _FakeRepositoryModel.get_or_create_calls[-1]
    assert created_kwargs["defaults"]["lakefs_repo"] == "m-owner-demo-gen1", (
        "An allocated id that is not persisted makes the repository unreachable"
    )
    create_calls = [kwargs for name, kwargs in client.calls if name == "create_repository"]
    assert create_calls[-1]["name"] == "m-owner-demo-gen1"
    assert create_calls[-1]["storage_namespace"].endswith("/m-owner-demo-gen1"), (
        "Storage namespace must follow the allocated id, not the derived one"
    )


@pytest.mark.asyncio
async def test_migrate_lakefs_repository_waits_for_the_old_repo_to_disappear(monkeypatch):
    """Stage 2: LakeFS acks DELETE before the repository record is gone, so the
    freed id stays taken. Waiting here means the common case never has to
    surface a conflict to the client at all.
    """
    client = _FakeClient()
    # Still present twice, then finally deleted.
    client.repository_exists_values = [True, True, False]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(repo_crud.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_a: SimpleNamespace(full_id="owner/from", lakefs_repo="from-repo"))
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("to-repo"))
    monkeypatch.setattr(repo_crud, "delete_objects_with_prefix", lambda bucket, prefix: _async_return(0))
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")

    client.list_payloads = [{"results": [], "pagination": {"has_more": False}}]
    await repo_crud._migrate_lakefs_repository(
        "model", "owner/from", "owner/to",
        from_lakefs_repo="from-repo",
    )

    exists_calls = [name for name, _ in client.calls if name == "repository_exists"]
    assert len(exists_calls) == 3, "must poll until the old repository is really gone"
    assert sleeps, "polling must yield between attempts"

    delete_index = [i for i, (name, _) in enumerate(client.calls) if name == "delete_repository"][0]
    first_exists_index = [i for i, (name, _) in enumerate(client.calls) if name == "repository_exists"][0]
    assert delete_index < first_exists_index, "the wait must follow the delete"


@pytest.mark.asyncio
async def test_migrate_lakefs_repository_wait_is_bounded_and_non_fatal(monkeypatch):
    """A repository whose cleanup outlives the bound (large history) must not
    fail the move - the rename itself already succeeded.
    """
    client = _FakeClient()
    sleeps = []

    class _NeverDeleted(_FakeClient):
        async def repository_exists(self, repo_name):
            self.calls.append(("repository_exists", repo_name))
            return True

    client = _NeverDeleted()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(repo_crud.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_a: SimpleNamespace(full_id="owner/from", lakefs_repo="from-repo"))
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("to-repo"))
    monkeypatch.setattr(repo_crud, "delete_objects_with_prefix", lambda bucket, prefix: _async_return(0))
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")

    client.list_payloads = [{"results": [], "pagination": {"has_more": False}}]
    await repo_crud._migrate_lakefs_repository(
        "model", "owner/from", "owner/to",
        from_lakefs_repo="from-repo",
    )

    exists_calls = [name for name, _ in client.calls if name == "repository_exists"]
    assert exists_calls, "must have tried"
    assert len(exists_calls) <= repo_crud.LAKEFS_DELETION_WAIT_MAX_ATTEMPTS, (
        "the wait must be bounded so a large repo cannot hang the request"
    )


def test_is_lakefs_repo_id_taken_error_prefers_the_structured_status_code():
    """LakeFSRestClient raises httpx.HTTPStatusError, so the status is available
    structurally. Matching "409" in the message alone would misfire on a
    repository name or URL that merely contains those digits.
    """

    class _Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    class _Err(RuntimeError):
        def __init__(self, message, status_code):
            super().__init__(message)
            self.response = _Resp(status_code)

    assert (
        repo_crud._is_lakefs_repo_id_taken_error(
            _Err('{"message":"error creating repository: not unique"}', 409)
        )
        is True
    )
    # Same wording, different status: not our retryable case.
    assert (
        repo_crud._is_lakefs_repo_id_taken_error(
            _Err('{"message":"error creating repository: not unique"}', 400)
        )
        is False
    )
    # "409" appearing only inside the repository name must not qualify.
    assert (
        repo_crud._is_lakefs_repo_id_taken_error(
            RuntimeError("LakeFS API error 500 for repo d-owner-model409-abc: boom")
        )
        is False
    )


@pytest.mark.asyncio
async def test_wait_for_lakefs_repo_deletion_reports_states_and_survives_probe_errors(
    monkeypatch,
):
    sleeps = []

    class _Client:
        def __init__(self, values):
            self.values = list(values)
            self.calls = 0

        async def repository_exists(self, repo_name):
            self.calls += 1
            value = self.values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(repo_crud.asyncio, "sleep", fake_sleep)

    gone = await repo_crud._wait_for_lakefs_repo_deletion(
        _Client([True, False]), "old-repo"
    )
    assert gone is True

    # A probe failure must not fail the move; the data is already migrated.
    survived = await repo_crud._wait_for_lakefs_repo_deletion(
        _Client([RuntimeError("lakefs unreachable")]), "old-repo"
    )
    assert survived is False

    never = _Client([True] * repo_crud.LAKEFS_DELETION_WAIT_MAX_ATTEMPTS)
    timed_out = await repo_crud._wait_for_lakefs_repo_deletion(never, "old-repo")
    assert timed_out is False
    assert never.calls == repo_crud.LAKEFS_DELETION_WAIT_MAX_ATTEMPTS
    # One sleep between attempts, none after the final one: 1 for the first
    # client (it sleeps once, then sees the repo gone) plus MAX - 1 here.
    assert len(sleeps) == 1 + (repo_crud.LAKEFS_DELETION_WAIT_MAX_ATTEMPTS - 1)


@pytest.mark.asyncio
async def test_create_repo_reports_allocation_failure_as_a_shaped_server_error(
    monkeypatch,
):
    """A LakeFS outage during id allocation must keep the HF error shape.

    Allocation probes LakeFS before creating anything, so it is a new place the
    request can fail. Letting that exception escape would return a bare 500 with
    no X-Error-Code, losing the header protocol the rest of the API follows.
    """
    user = SimpleNamespace(username="owner")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("lakefs unreachable")

    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(
        repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: _FakeClient())
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", _boom)
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", lambda *_args: None)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )

    assert response.status_code == 500
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.SERVER_ERROR
    assert not _FakeRepositoryModel.get_or_create_calls


@pytest.mark.asyncio
async def test_move_repo_reports_a_lost_rename_race_as_exists(monkeypatch):
    """Two moves (or a move and a create) racing for the same target: the unique
    (repo_type, namespace, name) index rejects the loser, which must see the
    ordinary "already exists" answer rather than a 500."""
    repo_row = SimpleNamespace(
        private=False,
        repo_type="model",
        full_id="owner/from",
        lakefs_repo="m-owner-from",
    )

    def _lost_race(**_kwargs):
        raise repo_crud.IntegrityError("duplicate key value violates unique constraint")

    monkeypatch.setattr(repo_crud, "check_repo_delete_permission", lambda repo, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None)
    monkeypatch.setattr(
        repo_crud,
        "get_repository",
        lambda repo_type, namespace, name: repo_row if (namespace, name) == ("owner", "from") else None,
    )
    monkeypatch.setattr(repo_crud, "_update_repository_database_records", _lost_race)
    monkeypatch.setattr(repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext({})))
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    _FakeRepositoryModel.reset()

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/to", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS


@pytest.mark.asyncio
async def test_create_repo_reports_a_concurrent_winner_as_exists_not_retry(monkeypatch):
    """A lost create race is "already exists", not "retry shortly".

    Two parallel creates both find generation 0 free, so the loser gets the same
    LakeFS `409 not unique` as the recycling case. But the name is now taken for
    good, not transiently: answering with the retryable sentence would make the
    client spin (and cost it the pacing hold) before it eventually learns the
    repo exists. Distinguish the two by re-checking the DB.
    """
    user = SimpleNamespace(username="owner")
    sleeps = []

    class _IdTakenClient(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            raise RuntimeError(
                "LakeFS API error 409 Conflict: "
                '{"message":"error creating repository: not unique"}'
            )

    # Absent on the pre-flight check, present by the time the create fails -
    # exactly what a concurrent winner looks like.
    lookups = {"count": 0}

    def fake_get_repository(*_args):
        lookups["count"] += 1
        return None if lookups["count"] == 1 else SimpleNamespace()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(repo_crud.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    monkeypatch.setattr(
        repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None
    )
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: _IdTakenClient())
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    monkeypatch.setattr(repo_crud, "get_repository", fake_get_repository)
    monkeypatch.setattr(repo_crud, "normalize_name", lambda name: name.lower())

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=user
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    body = json.loads(bytes(response.body))
    assert body["url"], "create_repo(exist_ok=True) reads url off the 409 body"
    assert repo_crud.LAKEFS_CONFLICT_RETRY_MESSAGE not in bytes(response.body).decode(), (
        "a permanently taken name must not be advertised as retryable"
    )
    assert sleeps == [], "no need to pace a client that should stop retrying"


def _move_env(monkeypatch, repo_row, siblings):
    """Stubs for move tests: `siblings` are the target namespace's repositories."""
    updates = []
    monkeypatch.setattr(repo_crud, "check_repo_delete_permission", lambda repo, user, is_admin=False: None)
    monkeypatch.setattr(repo_crud, "check_namespace_permission", lambda namespace, user, is_admin=False: None)
    monkeypatch.setattr(
        repo_crud,
        "get_repository",
        lambda repo_type, namespace, name: repo_row if (namespace, name) == ("owner", repo_row.name) else None,
    )
    monkeypatch.setattr(repo_crud, "_update_repository_database_records", lambda **kwargs: updates.append(kwargs))
    monkeypatch.setattr(repo_crud, "db", SimpleNamespace(atomic=lambda: _AtomicContext({})))
    monkeypatch.setattr(repo_crud, "Repository", _FakeRepositoryModel)
    _FakeRepositoryModel.reset()
    _FakeRepositoryModel.select_query = _Query(items=siblings)
    return updates


@pytest.mark.asyncio
async def test_move_repo_rejects_a_target_that_normalizes_to_an_existing_name(monkeypatch):
    """Create refuses names that differ only by case, '-' or '_' from an existing
    repository; a move must not be a way around that."""
    repo_row = SimpleNamespace(id=1, name="from", private=False, repo_type="model", full_id="owner/from", lakefs_repo="m-owner-from")
    existing = SimpleNamespace(id=2, name="demo_model")
    updates = _move_env(monkeypatch, repo_row, [repo_row, existing])

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/Demo-Model", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert "demo_model" in json.loads(bytes(response.body))["error"]
    assert updates == []


@pytest.mark.asyncio
async def test_move_repo_allows_renaming_a_repository_to_a_variant_of_its_own_name(monkeypatch):
    """Changing only case or separators of a repository's own name is allowed:
    the only normalized match is the repository being renamed."""
    repo_row = SimpleNamespace(id=1, name="demo-model", private=False, repo_type="model", full_id="owner/demo-model", lakefs_repo="m-owner-demo-model")
    updates = _move_env(monkeypatch, repo_row, [repo_row])

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/demo-model", toRepo="owner/Demo_Model", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert response["success"] is True
    assert updates[-1]["to_name"] == "Demo_Model"


@pytest.mark.asyncio
async def test_move_repo_pins_the_derived_lakefs_id_of_a_legacy_row(monkeypatch):
    """A row from before migration 016 has no stored LakeFS id and derives it from
    its repo id. After a rename it would derive from the *new* id - a repository
    that does not exist - so the move must store the old derivation explicitly."""
    repo_row = SimpleNamespace(id=1, name="legacy", private=False, repo_type="model", full_id="owner/legacy", lakefs_repo=None)
    updates = _move_env(monkeypatch, repo_row, [repo_row])

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/legacy", toRepo="owner/renamed", type="model"),
        auth=(SimpleNamespace(username="owner"), False),
    )

    assert response["success"] is True
    assert updates[-1]["to_lakefs_repo"] == lakefs_repo_name("model", "owner/legacy")
    assert updates[-1]["to_lakefs_repo"] != lakefs_repo_name("model", "owner/renamed")


class _RowAlreadyThere(_FakeRepositoryModel):
    @classmethod
    def get_or_create(cls, **kwargs):
        cls.get_or_create_calls.append(kwargs)
        return SimpleNamespace(full_id=kwargs["full_id"]), False


class _RowInsertFails(_FakeRepositoryModel):
    @classmethod
    def get_or_create(cls, **kwargs):
        raise RuntimeError("database connection lost")


@pytest.mark.asyncio
async def test_create_repo_reports_a_row_claimed_during_the_lakefs_create_as_exists(monkeypatch):
    """Two concurrent creates of one name: the loser's LakeFS create can succeed
    (on another id) before the winner's row is visible. The loser must then drop
    its own LakeFS repository and answer "exists", not report success."""
    client = _FakeClient()
    _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0"])
    monkeypatch.setattr(repo_crud, "Repository", _RowAlreadyThere)
    _RowAlreadyThere.get_or_create_calls = []

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert ("delete_repository", {"repository": "m-owner-demo-gen0", "force": True}) in client.calls


@pytest.mark.asyncio
async def test_create_repo_removes_its_lakefs_repo_when_the_row_insert_fails(monkeypatch):
    """A LakeFS repository without a row is unreachable: remove it on failure."""
    client = _FakeClient()
    _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0"])
    monkeypatch.setattr(repo_crud, "Repository", _RowInsertFails)

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response.status_code == 500
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.SERVER_ERROR
    assert ("delete_repository", {"repository": "m-owner-demo-gen0", "force": True}) in client.calls


@pytest.mark.asyncio
async def test_create_repo_row_failure_survives_lakefs_cleanup_errors(monkeypatch):
    """Removing the orphan is best effort; its failure must not mask the error."""
    client = _FakeClient()
    client.raise_on["delete_repository"] = RuntimeError("lakefs down")
    _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0"])
    monkeypatch.setattr(repo_crud, "Repository", _RowInsertFails)

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response.status_code == 500
