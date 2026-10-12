"""An object stored only for repositories the caller cannot read is not reused for another repository.

The batch route does not answer "already stored" for it, and an upload for it
never lands on the shared key: the bytes go to a key private to the repository
and count once they hash to the object id (``kohakuhub.lfs_scope``). Driven
through the real application against real storage.

``outsider`` has no access to anything and commits to a repository of their
own; the object belongs to ``acme-labs/private-dataset``.
"""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlsplit

import httpx
import pytest

from test.kohakuhub.api.commit.routers.test_lfs_commit_scope import _commit_pointer
from test.kohakuhub.api.git.routers.test_lfs_repository_scope import (
    LFS_HEADERS,
    _file,
    _private,
)



@pytest.fixture
def bucket(backend_test_state):
    return backend_test_state.modules.config_module.cfg.s3.bucket


@pytest.fixture
def s3(backend_test_state):
    return backend_test_state.s3_client


def _shared_key(oid):
    return f"lfs/{oid[:2]}/{oid[2:4]}/{oid}"


def _claim_key(repo_id, oid):
    from kohakuhub.db import Repository

    namespace, name = repo_id.split("/")
    repo = Repository.get((Repository.namespace == namespace) & (Repository.name == name))
    return f"lfs-claim/{repo.id}/{oid}"


def _exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


async def _stored_for_others(s3, bucket, content=None):
    """The object sits on the shared key, held by the private dataset only.

    Returns ``(oid, content)``; the content is unique per call because the rows
    a test writes outlive it."""
    content = content or b"private shard " + uuid.uuid4().bytes
    oid = hashlib.sha256(content).hexdigest()
    s3.put_object(Bucket=bucket, Key=_shared_key(oid), Body=content)
    _file(_private(), oid)
    return oid, content


async def _own_repo(client):
    name = f"claims-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/repos/create", json={"type": "model", "name": name, "private": False}
    )
    assert response.status_code == 200, response.text
    return f"outsider/{name}"


async def _batch(client, repo_id, oid, size):
    response = await client.post(
        f"/{repo_id}.git/info/lfs/objects/batch",
        json={
            "operation": "upload",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": size}],
        },
        headers=LFS_HEADERS,
    )
    assert response.status_code == 200, response.text
    (obj,) = response.json()["objects"]
    return obj


def _follow_up(href):
    parts = urlsplit(href)
    return f"{parts.path}?{parts.query}"


def _grant(repo_id, oid):
    from kohakuhub.db import LfsGrant, Repository

    namespace, name = repo_id.split("/")
    repo = Repository.get((Repository.namespace == namespace) & (Repository.name == name))
    return LfsGrant.get_or_none((LfsGrant.repository == repo) & (LfsGrant.sha256 == oid))


async def _upload_single_part(obj, content):
    async with httpx.AsyncClient() as client:
        response = await client.put(obj["actions"]["upload"]["href"], content=content)
    assert response.status_code == 200, response.text


# --- the batch answer ------------------------------------------------------


async def test_the_batch_does_not_say_already_stored_and_targets_a_private_key(
    outsider_client, s3, bucket
):
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)

    obj = await _batch(outsider_client, repo_id, oid, len(secret))

    assert "error" not in obj
    href = obj["actions"]["upload"]["href"]
    assert _claim_key(repo_id, oid) in href
    assert _shared_key(oid) not in href
    assert _grant(repo_id, oid).kind == "claim"


async def test_a_large_object_gets_a_private_multipart_upload(
    outsider_client, s3, bucket, monkeypatch
):
    from kohakuhub.config import cfg

    monkeypatch.setattr(cfg.app, "lfs_multipart_threshold_bytes", 1)
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)

    obj = await _batch(outsider_client, repo_id, oid, len(secret))

    header = obj["actions"]["upload"]["header"]
    assert "chunk_size" in header
    assert _claim_key(repo_id, oid) in header["1"]
    assert _shared_key(oid) not in header["1"]


async def test_an_object_stored_nowhere_still_uploads_to_the_shared_key(
    outsider_client, bucket
):
    content = b"a new object nobody stores yet"
    oid = hashlib.sha256(content).hexdigest()
    repo_id = await _own_repo(outsider_client)

    obj = await _batch(outsider_client, repo_id, oid, len(content))

    assert _shared_key(oid) in obj["actions"]["upload"]["href"]
    assert _grant(repo_id, oid).kind == "upload"


# --- proving the claim -----------------------------------------------------


async def test_verify_proves_the_claim_and_the_shared_object_is_untouched(
    outsider_client, s3, bucket
):
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)
    obj = await _batch(outsider_client, repo_id, oid, len(secret))

    await _upload_single_part(obj, secret)
    verified = await outsider_client.post(
        _follow_up(obj["actions"]["verify"]["href"]), json={"oid": oid, "size": len(secret)}
    )

    assert verified.status_code == 200, verified.text
    assert _grant(repo_id, oid).proven is True
    assert not _exists(s3, bucket, _claim_key(repo_id, oid))
    assert s3.get_object(Bucket=bucket, Key=_shared_key(oid))["Body"].read() == secret
    committed = await _commit_pointer(outsider_client, repo_id, oid, len(secret), "mine.bin")
    assert committed.status_code == 200, committed.text


async def test_bytes_that_do_not_hash_to_the_id_prove_nothing_and_are_removed(
    outsider_client, s3, bucket
):
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)
    obj = await _batch(outsider_client, repo_id, oid, len(secret))

    await _upload_single_part(obj, b"x" * len(secret))
    verified = await outsider_client.post(
        _follow_up(obj["actions"]["verify"]["href"]), json={"oid": oid, "size": len(secret)}
    )

    assert verified.status_code == 404
    assert _grant(repo_id, oid).proven is False
    assert not _exists(s3, bucket, _claim_key(repo_id, oid))
    assert s3.get_object(Bucket=bucket, Key=_shared_key(oid))["Body"].read() == secret
    refused = await _commit_pointer(outsider_client, repo_id, oid, len(secret))
    assert refused.status_code == 400


async def test_a_claim_with_nothing_uploaded_proves_nothing(outsider_client, s3, bucket):
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)
    obj = await _batch(outsider_client, repo_id, oid, len(secret))

    verified = await outsider_client.post(
        _follow_up(obj["actions"]["verify"]["href"]), json={"oid": oid, "size": len(secret)}
    )

    assert verified.status_code == 404
    refused = await _commit_pointer(outsider_client, repo_id, oid, len(secret))
    assert refused.status_code == 400


async def test_the_commit_proves_the_claim_when_the_client_never_verifies(
    outsider_client, s3, bucket
):
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)
    obj = await _batch(outsider_client, repo_id, oid, len(secret))

    await _upload_single_part(obj, secret)
    committed = await _commit_pointer(outsider_client, repo_id, oid, len(secret), "mine.bin")

    assert committed.status_code == 200, committed.text
    assert _grant(repo_id, oid).proven is True


async def test_a_multipart_claim_completes_into_the_private_key_and_is_proven(
    outsider_client, s3, bucket, monkeypatch
):
    from kohakuhub.config import cfg

    monkeypatch.setattr(cfg.app, "lfs_multipart_threshold_bytes", 1)
    oid, secret = await _stored_for_others(s3, bucket)
    repo_id = await _own_repo(outsider_client)
    obj = await _batch(outsider_client, repo_id, oid, len(secret))
    header = obj["actions"]["upload"]["header"]

    async with httpx.AsyncClient() as client:
        part = await client.put(header["1"], content=secret)
    assert part.status_code == 200, part.text
    completed = await outsider_client.post(
        _follow_up(obj["actions"]["upload"]["href"]),
        json={"oid": oid, "parts": [{"PartNumber": 1, "ETag": part.headers["ETag"]}]},
    )
    assert completed.status_code == 200, completed.text
    assert _exists(s3, bucket, _claim_key(repo_id, oid))

    verified = await outsider_client.post(
        _follow_up(obj["actions"]["verify"]["href"]), json={"oid": oid, "size": len(secret)}
    )

    assert verified.status_code == 200, verified.text
    assert _grant(repo_id, oid).proven is True
    assert s3.get_object(Bucket=bucket, Key=_shared_key(oid))["Body"].read() == secret


# --- verify is no existence oracle ----------------------------------------


async def test_verify_answers_a_foreign_object_like_a_missing_one(outsider_client, s3, bucket):
    held, _ = await _stored_for_others(s3, bucket, b"verify oracle shard " + uuid.uuid4().bytes)
    nowhere = hashlib.sha256(b"verify never stored").hexdigest()
    repo_id = await _own_repo(outsider_client)

    async def verify(oid):
        return await outsider_client.post(
            f"/api/{repo_id}.git/info/lfs/verify", json={"oid": oid, "size": 5}
        )

    first, second = await verify(held), await verify(nowhere)

    assert first.status_code == second.status_code == 404
    assert first.json() == second.json()
