"""Unit tests for repository CRUD routes and helpers, on real repository rows.

The LakeFS client, the LakeFS id allocator and the S3 helpers are external
services and stay fakes. Repository lookup, uniqueness, owners, quotas and
the writes of create, delete and move are real SQL.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import kohakuhub.api.repo.routers.crud as repo_crud
import kohakuhub.api.operation_capabilities as operation_capabilities
from kohakuhub.db import BackgroundTask, File, Repository
from kohakuhub.storage_cleanup import PURGE_KIND
from kohakuhub.utils.lakefs import lakefs_repo_name
from test.kohakuhub.support.db import table_missing
from test.kohakuhub.support.factories import make_org, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


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

    async def get_branch(self, **kwargs):
        self.calls.append(("get_branch", kwargs))
        return {"commit_id": "c0"}


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()


def _repo_count(full_id):
    return Repository.select().where(Repository.full_id == full_id).count()


@pytest.mark.asyncio
async def test_create_repo_covers_conflicts_lakefs_failure_and_success(monkeypatch):
    owner = make_user("owner")
    client = _FakeClient()
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    demo = make_repo(owner, "demo-model")
    # Only the normalized name matches: "Demo_Model" normalizes like "demo-model"
    conflict = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="Demo_Model"), user=owner
    )
    # huggingface_hub's create_repo(exist_ok=True) only shortcuts on 409, so the
    # conflict response now uses 409 Conflict with a JSON body carrying `url`.
    assert conflict.status_code == 409
    assert conflict.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert json.loads(bytes(conflict.body)).get("url")

    exact_conflict = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )
    assert exact_conflict.status_code == 409
    assert json.loads(bytes(exact_conflict.body)).get("url")

    demo.delete_instance()
    client.raise_on["create_repository"] = RuntimeError("lakefs failed")
    lakefs_error = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )
    assert lakefs_error.status_code == 500
    assert _repo_count("owner/demo-model") == 0, "a failed LakeFS create leaves no row"

    client.raise_on.pop("create_repository", None)
    success = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )
    assert success["repo_id"] == "owner/demo-model"
    created = Repository.get(full_id="owner/demo-model")
    assert created.lakefs_repo == lakefs_repo_name("model", "owner/demo-model")
    # Usage counting starts from main's head commit, which the row records
    assert created.main_counted_commit == "c0"


@pytest.mark.asyncio
async def test_create_repo_succeeds_when_usage_counting_cannot_start(monkeypatch):
    """Usage counting is best effort: a failed head lookup leaves the row for a recount."""
    owner = make_user("owner")

    class _NoHeadClient(_FakeClient):
        async def get_branch(self, **kwargs):
            raise RuntimeError("lakefs unreachable")

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: _NoHeadClient())
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )

    assert response["repo_id"] == "owner/demo-model"
    assert Repository.get(full_id="owner/demo-model").main_counted_commit is None


@pytest.mark.asyncio
async def test_delete_repo_covers_admin_validation_not_found_and_failures(
    monkeypatch, db_scope
):
    owner = make_user("owner")

    with pytest.raises(HTTPException) as admin_missing_org:
        await repo_crud.delete_repo(
            repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
            auth=(None, True),
        )
    assert admin_missing_org.value.status_code == 400

    not_found = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(owner, False),
    )
    assert not_found.status_code == 404

    # The row goes at once; its storage is purged by a task scheduled with it
    make_repo(owner, "demo-model")
    success = await repo_crud.delete_repo(
        repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
        auth=(owner, False),
    )
    assert "deleted" in success["message"].lower()
    assert _repo_count("owner/demo-model") == 0
    purge = BackgroundTask.select().where(BackgroundTask.kind == PURGE_KIND)
    assert purge.count() == 1

    # A database failure while the row is being deleted: File cannot be read
    make_repo(owner, "demo-model")
    with table_missing(db_scope, File):
        db_failure = await repo_crud.delete_repo(
            repo_crud.DeleteRepoPayload(type="model", name="demo-model"),
            auth=(owner, False),
        )
    assert db_failure.status_code == 500
    assert _repo_count("owner/demo-model") == 1, "a failed delete keeps the row"


def test_update_repository_database_records_covers_same_and_cross_namespace_moves():
    owner = make_user("owner")
    repo_row = make_repo(owner, "from", quota_bytes=100, used_bytes=50)

    repo_crud._update_repository_database_records(
        repo_row=repo_row,
        from_id="owner/from",
        to_id="owner/to",
        from_namespace="owner",
        to_namespace="owner",
        to_name="to",
        moving_namespace=False,
        to_lakefs_repo="m-owner-to",
    )
    same = Repository.get_by_id(repo_row.id)
    assert (same.full_id, same.quota_bytes) == ("owner/to", 100)
    assert same.lakefs_repo == "m-owner-to", (
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
        to_lakefs_repo="m-org-team-to",
    )
    moved = Repository.get_by_id(repo_row.id)
    assert moved.quota_bytes is None
    # Its usage goes along with the row: nothing to write
    assert moved.used_bytes == 50
    assert (moved.namespace, moved.lakefs_repo) == ("org-team", "m-org-team-to")


@pytest.mark.asyncio
async def test_move_repo_covers_validation_quota_and_metadata_only_success(monkeypatch):
    """A move renames the KHub row only; LakeFS and S3 are never touched (#107).

    Recreating the LakeFS repository used to keep only the main head, dropping
    history, other branches and tags. Since migration 016 the LakeFS id lives on
    the row, so the row can keep it across a rename.
    """
    owner = make_user("owner")
    org = make_org("other", admin=owner)
    repo_row = make_repo(owner, "from", used_bytes=12, lakefs_repo="m-owner-from")

    def _forbidden(name):
        def _raise(*_args, **_kwargs):
            raise AssertionError(f"move must not call {name}")

        return _raise

    for name in (
        "allocate_lakefs_repo_name",
        "get_lakefs_client",
    ):
        monkeypatch.setattr(repo_crud, name, _forbidden(name))
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    bad_source = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="bad", toRepo="owner/to", type="model"),
        auth=(owner, False),
    )
    assert bad_source.status_code == 400

    bad_target = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="bad", type="model"),
        auth=(owner, False),
    )
    assert bad_target.status_code == 400

    not_found = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/ghost", toRepo="owner/to", type="model"),
        auth=(owner, False),
    )
    assert not_found.status_code == 404

    make_repo(owner, "to")
    exists = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/to", type="model"),
        auth=(owner, False),
    )
    assert exists.status_code == 409
    assert exists.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS

    # The target namespace must name an account (admin bypasses the namespace check)
    nowhere = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="nobody/to", type="model"),
        auth=(None, True),
    )
    assert nowhere.status_code == 404

    # The organization's public quota (the repository is public) is 10 bytes; the repository uses 12
    org.public_quota_bytes = 10
    org.save()
    with pytest.raises(HTTPException) as quota_error:
        await repo_crud.move_repo(
            repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="other/to", type="model"),
            auth=(owner, False),
        )
    assert quota_error.value.status_code == 400

    org.public_quota_bytes = None
    org.save()
    success = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="other/to", type="model"),
        auth=(owner, False),
    )
    assert success["success"] is True
    assert success["url"] == "https://hub.example.com/models/other/to"
    moved = Repository.get_by_id(repo_row.id)
    assert (moved.namespace, moved.name, moved.full_id) == ("other", "to", "other/to")
    # The row keeps pointing at the LakeFS repository that holds its data.
    assert moved.lakefs_repo == "m-owner-from"
    # It goes to the account the namespace names
    assert moved.owner_id == org.id


@pytest.mark.asyncio
async def test_disabled_squash_rejects_before_repository_lookup(monkeypatch):
    monkeypatch.setattr(
        operation_capabilities.cfg.app, "repository_squash_enabled", False
    )

    # Ordering check: the guard must answer before any lookup runs. A row cannot
    # show that, so the lookup is a spy that fails the test if it is reached.
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
    user = make_user("owner")

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
    assert _repo_count("owner/demo-model") == 1, (
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

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud,
        "resolve_lakefs_repo",
        lambda repo: f"{repo.repo_type}:{repo.full_id}",
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
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
    owner = make_user("owner")
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
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )

    assert response.status_code == 409
    assert "another conflicting operation is in progress" in bytes(response.body).decode()
    attempts = [name for name, _kwargs in client.calls if name == "create_repository"]
    assert len(attempts) == repo_crud.LAKEFS_CREATE_ATTEMPTS
    assert sleeps == [repo_crud.LAKEFS_RECYCLING_HOLD_SECONDS], (
        "hf_hub's retry loop has no backoff of its own, so the server supplies "
        "the delay by holding the response briefly"
    )
    assert _repo_count("owner/demo-model") == 0, (
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
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", fake_allocate)
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")
    return allocated


@pytest.mark.asyncio
async def test_create_repo_steps_over_a_taken_lakefs_id(monkeypatch):
    """An id that LakeFS reports taken at create time (still being deleted, or a
    probe race) is skipped automatically: the user gets their repository under a
    fresh LakeFS id instead of an error or a "retry later"."""
    owner = make_user("owner")

    class _FirstIdTaken(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            if kwargs["name"] == "m-owner-demo-gen0":
                raise RuntimeError('LakeFS API error 409: {"message":"error creating repository: not unique"}')

    client = _FirstIdTaken()
    allocated = _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0", "m-owner-demo-gen1"])

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=owner,
    )

    assert response["repo_id"] == "owner/demo-model"
    assert allocated == [set(), {"m-owner-demo-gen0"}]
    created = [kwargs["name"] for name, kwargs in client.calls if name == "create_repository"]
    assert created == ["m-owner-demo-gen0", "m-owner-demo-gen1"]
    assert created_storage_namespace(client) == "s3://hub-storage/m-owner-demo-gen1"
    assert Repository.get(full_id="owner/demo-model").lakefs_repo == "m-owner-demo-gen1"


def created_storage_namespace(client):
    return [kwargs for name, kwargs in client.calls if name == "create_repository"][-1][
        "storage_namespace"
    ]


@pytest.mark.asyncio
async def test_create_repo_steps_over_a_storage_namespace_it_cannot_heal(monkeypatch):
    """A leftover storage namespace that is not safe to delete used to fail the
    create with a 500; now the create moves on to a fresh LakeFS id."""
    owner = make_user("owner")

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
        user=owner,
    )

    assert response["repo_id"] == "owner/demo-model"
    assert allocated == [set(), {"m-owner-demo-gen0"}]
    assert Repository.get(full_id="owner/demo-model").lakefs_repo == "m-owner-demo-gen1"


@pytest.mark.asyncio
async def test_create_repo_persists_the_allocated_lakefs_repo_id(monkeypatch):
    owner = make_user("owner")
    client = _FakeClient()

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: client)
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo-gen1")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    result = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )

    assert result["repo_id"] == "owner/demo-model"
    # An allocated id that is not persisted makes the repository unreachable
    stored = Repository.get(full_id="owner/demo-model")
    assert stored.lakefs_repo == "m-owner-demo-gen1"
    create_calls = [kwargs for name, kwargs in client.calls if name == "create_repository"]
    assert create_calls[-1]["name"] == "m-owner-demo-gen1"
    assert create_calls[-1]["storage_namespace"].endswith("/m-owner-demo-gen1"), (
        "Storage namespace must follow the allocated id, not the derived one"
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
async def test_create_repo_reports_allocation_failure_as_a_shaped_server_error(
    monkeypatch,
):
    """A LakeFS outage during id allocation must keep the HF error shape.

    Allocation probes LakeFS before creating anything, so it is a new place the
    request can fail. Letting that exception escape would return a bare 500 with
    no X-Error-Code, losing the header protocol the rest of the API follows.
    """
    owner = make_user("owner")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("lakefs unreachable")

    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: _FakeClient())
    monkeypatch.setattr(repo_crud, "allocate_lakefs_repo_name", _boom)
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )

    assert response.status_code == 500
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.SERVER_ERROR
    assert _repo_count("owner/demo-model") == 0


@pytest.mark.asyncio
async def test_move_repo_reports_a_lost_rename_race_as_exists(monkeypatch, db_scope):
    """Two moves (or a move and a create) racing for the same target: the unique
    (repo_type, namespace, name) index rejects the loser, which must see the
    ordinary "already exists" answer rather than a 500."""
    owner = make_user("owner")
    make_repo(owner, "from")

    # The rename runs in the test database's transaction, so its savepoint is real
    monkeypatch.setattr(repo_crud, "db", db_scope)
    real_update = repo_crud._update_repository_database_records

    def _winner_lands_first(**kwargs):
        # A concurrent create takes the target name after the checks and before the rename
        make_repo(owner, "to")
        return real_update(**kwargs)

    monkeypatch.setattr(repo_crud, "_update_repository_database_records", _winner_lands_first)

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/to", type="model"),
        auth=(owner, False),
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert _repo_count("owner/from") == 1, "the losing rename changes nothing"


@pytest.mark.asyncio
async def test_create_repo_reports_a_concurrent_winner_as_exists_not_retry(monkeypatch):
    """A lost create race is "already exists", not "retry shortly".

    Two parallel creates both find generation 0 free, so the loser gets the same
    LakeFS `409 not unique` as the recycling case. But the name is now taken for
    good, not transiently: answering with the retryable sentence would make the
    client spin (and cost it the pacing hold) before it eventually learns the
    repo exists. Distinguish the two by re-checking the DB.
    """
    owner = make_user("owner")
    sleeps = []

    class _IdTakenClient(_FakeClient):
        async def create_repository(self, **kwargs):
            self.calls.append(("create_repository", kwargs))
            # The concurrent winner's row lands while this create is in flight
            make_repo(owner, "demo-model")
            raise RuntimeError(
                "LakeFS API error 409 Conflict: "
                '{"message":"error creating repository: not unique"}'
            )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(repo_crud.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(repo_crud, "get_lakefs_client", lambda: _IdTakenClient())
    monkeypatch.setattr(
        repo_crud, "allocate_lakefs_repo_name", lambda *a, **k: _async_return("m-owner-demo")
    )
    monkeypatch.setattr(repo_crud.cfg.s3, "bucket", "hub-storage")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"), user=owner
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    body = json.loads(bytes(response.body))
    assert body["url"], "create_repo(exist_ok=True) reads url off the 409 body"
    assert repo_crud.LAKEFS_CONFLICT_RETRY_MESSAGE not in bytes(response.body).decode(), (
        "a permanently taken name must not be advertised as retryable"
    )
    assert sleeps == [], "no need to pace a client that should stop retrying"


@pytest.mark.asyncio
async def test_move_repo_rejects_a_target_that_normalizes_to_an_existing_name(monkeypatch):
    """Create refuses names that differ only by case, '-' or '_' from an existing
    repository; a move must not be a way around that."""
    owner = make_user("owner")
    repo_row = make_repo(owner, "from")
    make_repo(owner, "demo_model")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/from", toRepo="owner/Demo-Model", type="model"),
        auth=(owner, False),
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert "demo_model" in json.loads(bytes(response.body))["error"]
    assert Repository.get_by_id(repo_row.id).full_id == "owner/from"


@pytest.mark.asyncio
async def test_move_repo_allows_renaming_a_repository_to_a_variant_of_its_own_name(monkeypatch):
    """Changing only case or separators of a repository's own name is allowed:
    the only normalized match is the repository being renamed."""
    owner = make_user("owner")
    repo_row = make_repo(owner, "demo-model")
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/demo-model", toRepo="owner/Demo_Model", type="model"),
        auth=(owner, False),
    )

    assert response["success"] is True
    assert Repository.get_by_id(repo_row.id).name == "Demo_Model"


@pytest.mark.asyncio
async def test_move_repo_pins_the_derived_lakefs_id_of_a_legacy_row(monkeypatch):
    """A row from before migration 016 has no stored LakeFS id and derives it from
    its repo id. After a rename it would derive from the *new* id - a repository
    that does not exist - so the move must store the old derivation explicitly."""
    owner = make_user("owner")
    repo_row = make_repo(owner, "legacy", lakefs_repo=None)
    monkeypatch.setattr(repo_crud.cfg.app, "base_url", "https://hub.example.com")

    response = await repo_crud.move_repo(
        repo_crud.MoveRepoPayload(fromRepo="owner/legacy", toRepo="owner/renamed", type="model"),
        auth=(owner, False),
    )

    assert response["success"] is True
    stored = Repository.get_by_id(repo_row.id).lakefs_repo
    assert stored == lakefs_repo_name("model", "owner/legacy")
    assert stored != lakefs_repo_name("model", "owner/renamed")


@pytest.mark.asyncio
async def test_create_repo_reports_a_row_claimed_during_the_lakefs_create_as_exists(monkeypatch):
    """Two concurrent creates of one name: the loser's LakeFS create can succeed
    (on another id) before the winner's row is visible. The loser must then drop
    its own LakeFS repository and answer "exists", not report success."""
    owner = make_user("owner")

    class _WinnerLandsDuringCreate(_FakeClient):
        async def create_repository(self, **kwargs):
            await super().create_repository(**kwargs)
            # The winner's row is committed while this create is in flight
            make_repo(owner, "demo-model")
            return {"ok": True}

    client = _WinnerLandsDuringCreate()
    _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0"])

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=owner,
    )

    assert response.status_code == 409
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.REPO_EXISTS
    assert ("delete_repository", {"repository": "m-owner-demo-gen0", "force": True}) in client.calls


@pytest.mark.asyncio
async def test_create_repo_removes_its_lakefs_repo_when_the_row_insert_fails(monkeypatch):
    """A LakeFS repository without a row is unreachable: remove it on failure.

    No account is named "owner", so the row has no owner and the insert hits the
    real NOT NULL constraint on `owner_id`.
    """
    client = _FakeClient()
    _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0"])

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response.status_code == 500
    assert response.headers.get("x-error-code") == repo_crud.HFErrorCode.SERVER_ERROR
    assert ("delete_repository", {"repository": "m-owner-demo-gen0", "force": True}) in client.calls
    assert _repo_count("owner/demo-model") == 0


@pytest.mark.asyncio
async def test_create_repo_row_failure_survives_lakefs_cleanup_errors(monkeypatch):
    """Removing the orphan is best effort; its failure must not mask the error."""
    client = _FakeClient()
    client.raise_on["delete_repository"] = RuntimeError("lakefs down")
    _create_repo_env(monkeypatch, client, ["m-owner-demo-gen0"])

    response = await repo_crud.create_repo(
        repo_crud.CreateRepoPayload(type="model", name="demo-model"),
        user=SimpleNamespace(username="owner"),
    )

    assert response.status_code == 500
