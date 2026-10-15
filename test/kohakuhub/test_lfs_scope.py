"""Who may reference a stored LFS object, on real rows and real storage (``kohakuhub.lfs_scope``)."""

from __future__ import annotations

import hashlib
import importlib
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from kohakuhub import lakefs_rest_client


def _live(module):
    return importlib.import_module(module)


@pytest.fixture
def m(prepared_backend_test_state):
    lakefs_rest_client._singleton_client = None
    ns = SimpleNamespace(
        scope=_live("kohakuhub.lfs_scope"),
        gc=_live("kohakuhub.lfs_gc"),
        db=_live("kohakuhub.db"),
        cleanup=_live("kohakuhub.storage_cleanup"),
        cfg=_live("kohakuhub.config").cfg,
        s3=_live("kohakuhub.utils.s3").get_s3_client(),
    )
    yield ns
    lakefs_rest_client._singleton_client = None


def _repo(m, full_id="owner/demo-model", repo_type="model"):
    namespace, name = full_id.split("/")
    return m.db.Repository.get(
        (m.db.Repository.repo_type == repo_type)
        & (m.db.Repository.namespace == namespace)
        & (m.db.Repository.name == name)
    )


def _user(m, name):
    return m.db.User.get(m.db.User.username == name)


def _unique():
    content = b"scope " + uuid.uuid4().bytes
    return hashlib.sha256(content).hexdigest(), content


def _hold(m, repo, oid):
    return m.db.File.create(
        repository=repo,
        owner_id=repo.owner_id,
        path_in_repo=f"{oid[:12]}.bin",
        sha256=oid,
        size=3,
        lfs=True,
        branch="main",
    )


def _put(m, key, content):
    m.s3.put_object(Bucket=m.cfg.s3.bucket, Key=key, Body=content)


def _has(m, key):
    try:
        m.s3.head_object(Bucket=m.cfg.s3.bucket, Key=key)
        return True
    except Exception:
        return False


# --- standing and readability ---------------------------------------------


def test_a_repository_has_standing_for_what_it_holds_or_was_granted(m):
    repo = _repo(m)
    held, _ = _unique()
    granted, _ = _unique()
    claimed, _ = _unique()
    stranger, _ = _unique()
    _hold(m, repo, held)
    m.scope.record_grant(repo, granted, "upload")
    m.scope.record_grant(repo, claimed, "claim")

    assert m.scope.has_standing(repo, held)
    assert m.scope.has_standing(repo, granted)
    assert not m.scope.has_standing(repo, claimed)  # a claim counts once it is proven
    assert not m.scope.has_standing(repo, stranger)

    m.db.LfsGrant.update(proven=True).where(m.db.LfsGrant.sha256 == claimed).execute()
    assert m.scope.has_standing(repo, claimed)


def test_an_upload_grant_replaces_a_claim_the_object_outlived(m):
    repo = _repo(m)
    oid, _ = _unique()
    m.scope.record_grant(repo, oid, "claim")

    assert m.scope.record_grant(repo, oid, "claim").kind == "claim"  # asked again: unchanged
    assert m.scope.record_grant(repo, oid, "upload").kind == "upload"
    assert m.scope.record_grant(repo, oid, "claim").kind == "upload"  # never downgraded
    assert m.db.LfsGrant.select().where(m.db.LfsGrant.sha256 == oid).count() == 1


def test_readable_by_follows_who_can_read_a_holding_repository(m):
    private = _repo(m, "acme-labs/private-dataset", "dataset")
    oid, _ = _unique()
    _hold(m, private, oid)

    assert m.scope.readable_by(_user(m, "member"), oid)  # a member of the owning org
    assert not m.scope.readable_by(_user(m, "outsider"), oid)
    assert not m.scope.readable_by(None, oid)

    public_oid, _ = _unique()
    _hold(m, _repo(m), public_oid)
    assert m.scope.readable_by(None, public_oid)  # public content is anybody's

    other = m.db.LFSObjectHistory.create(
        repository=private, path_in_repo="old.bin", sha256=(h := _unique()[0]), size=1, commit_id="c" * 40
    )
    assert other and m.scope.readable_by(_user(m, "member"), h)
    link = m.db.LfsHeadRef.create(
        repository=private, branch="main", path_in_repo="head.bin", sha256=(k := _unique()[0])
    )
    assert link and m.scope.readable_by(_user(m, "member"), k)


# --- proving a claim -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_is_proven_by_hashing_what_was_uploaded(m):
    repo = _repo(m)
    oid, content = _unique()
    m.scope.record_grant(repo, oid, "claim")
    key = m.gc.claim_key(repo.id, oid)
    _put(m, key, content)

    assert await m.scope.prove_claim(repo, oid) is True

    assert m.db.LfsGrant.get(m.db.LfsGrant.sha256 == oid).proven is True
    assert not _has(m, key)  # the private copy is gone
    assert await m.scope.prove_claim(repo, oid) is True  # already proven: nothing to read


@pytest.mark.asyncio
async def test_wrong_bytes_prove_nothing_and_are_removed(m):
    repo = _repo(m)
    oid, content = _unique()
    m.scope.record_grant(repo, oid, "claim")
    key = m.gc.claim_key(repo.id, oid)
    _put(m, key, content + b"!")

    assert await m.scope.prove_claim(repo, oid) is False

    assert m.db.LfsGrant.get(m.db.LfsGrant.sha256 == oid).proven is False
    assert not _has(m, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("hashes", [True, False], ids=["right bytes", "wrong bytes"])
async def test_a_copy_that_cannot_be_removed_does_not_change_what_the_proof_says(m, monkeypatch, hashes):
    """Removing the uploaded copy is clean-up, best effort (#10): the answer is what the hash says.

    Targeted mock: a store that refuses the delete cannot be produced against the real MinIO.
    """
    repo = _repo(m)
    oid, content = _unique()
    m.scope.record_grant(repo, oid, "claim")
    key = m.gc.claim_key(repo.id, oid)
    _put(m, key, content if hashes else content + b"!")

    async def refused(bucket, prefix):
        raise m.scope.S3DeleteFailed("S3 refused to delete 1 object(s)")

    monkeypatch.setattr(m.scope, "delete_objects_with_prefix", refused)

    assert await m.scope.prove_claim(repo, oid) is hashes
    assert m.db.LfsGrant.get(m.db.LfsGrant.sha256 == oid).proven is hashes
    assert _has(m, key)  # left behind for later; nothing may use it
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=key)


@pytest.mark.asyncio
async def test_there_is_nothing_to_prove_without_a_claim_or_an_upload(m):
    repo = _repo(m)
    never, _ = _unique()
    plain, _ = _unique()
    waiting, _ = _unique()
    m.scope.record_grant(repo, plain, "upload")
    m.scope.record_grant(repo, waiting, "claim")

    assert await m.scope.prove_claim(repo, never) is False  # no grant at all
    assert await m.scope.prove_claim(repo, plain) is False  # an upload grant is no claim
    assert await m.scope.prove_claim(repo, waiting) is False  # claimed, nothing uploaded yet


@pytest.mark.asyncio
async def test_may_reference_tries_standing_then_reading_then_the_claim(m):
    repo = _repo(m)
    outsider = _user(m, "outsider")
    held, _ = _unique()
    shared, _ = _unique()
    claimed, content = _unique()
    _hold(m, repo, held)
    _hold(m, _repo(m, "acme-labs/private-dataset", "dataset"), shared)
    m.scope.record_grant(repo, claimed, "claim")
    _put(m, m.gc.claim_key(repo.id, claimed), content)

    assert await m.scope.may_reference(repo, outsider, held)
    assert not await m.scope.may_reference(repo, outsider, shared)
    assert await m.scope.may_reference(repo, _user(m, "member"), shared)
    assert await m.scope.may_reference(repo, outsider, claimed)  # proven on the way


# --- expiry ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_stale_unproven_claims_expire(m):
    repo = _repo(m)
    stale, stale_bytes = _unique()
    fresh, fresh_bytes = _unique()
    proven, _ = _unique()
    upload, _ = _unique()
    old = m.db.utcnow() - m.cleanup.LFS_CLAIM_TTL - timedelta(hours=1)
    for oid, kind, created in (
        (stale, "claim", old),
        (fresh, "claim", m.db.utcnow()),
        (proven, "claim", old),
        (upload, "upload", old),
    ):
        m.db.LfsGrant.create(repository=repo, sha256=oid, kind=kind, created_at=created)
    m.db.LfsGrant.update(proven=True).where(m.db.LfsGrant.sha256 == proven).execute()
    _put(m, m.gc.claim_key(repo.id, stale), stale_bytes)
    _put(m, m.gc.claim_key(repo.id, fresh), fresh_bytes)

    await m.cleanup.expire_lfs_claims({})

    remaining = {g.sha256 for g in m.db.LfsGrant.select()}
    assert stale not in remaining
    assert {fresh, proven, upload} <= remaining
    assert not _has(m, m.gc.claim_key(repo.id, stale))
    assert _has(m, m.gc.claim_key(repo.id, fresh))


@pytest.mark.asyncio
async def test_expiry_with_nothing_stale_changes_nothing(m):
    before = m.db.LfsGrant.select().count()

    await m.cleanup.expire_lfs_claims({})

    assert m.db.LfsGrant.select().count() == before
