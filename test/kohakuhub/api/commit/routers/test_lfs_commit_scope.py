"""A commit may only reference an LFS object its repository is entitled to.

Objects are stored once for the whole instance, keyed by their sha256, so a
pointer in a commit must not become a way to attach an object that belongs to
somebody else. Driven through the real application against real storage; the
object is written straight into the bucket as an earlier upload would, and the
owning repository's rows go straight into the real database.

Roles come from the shared baseline: ``owner`` (public model
``owner/demo-model``; can also read the private dataset), ``member`` (admin of
``acme-labs``, which owns the private dataset ``acme-labs/private-dataset``),
``outsider`` (no access to anything; commits to a repository of their own).
"""

from __future__ import annotations

import hashlib
import re
import uuid

from test.kohakuhub.api.helpers import encode_ndjson
from test.kohakuhub.support.grants import grant_lfs_upload

NDJSON = {"Content-Type": "application/x-ndjson"}


def _store(backend_test_state, content: bytes) -> str:
    """Put ``content`` where an earlier upload leaves it; returns its oid."""
    oid = hashlib.sha256(content).hexdigest()
    backend_test_state.s3_client.put_object(
        Bucket=backend_test_state.modules.config_module.cfg.s3.bucket,
        Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}",
        Body=content,
    )
    return oid


def _private_dataset():
    from kohakuhub.db import Repository

    return Repository.get(
        (Repository.repo_type == "dataset")
        & (Repository.namespace == "acme-labs")
        & (Repository.name == "private-dataset")
    )


def _hold(repo, oid, size):
    """``repo`` references ``oid`` the way a finished commit leaves it."""
    from kohakuhub.db import File

    return File.create(
        repository=repo,
        owner_id=repo.owner_id,
        path_in_repo=f"{oid[:12]}.bin",
        sha256=oid,
        size=size,
        lfs=True,
        branch="main",
    )


async def _own_repo(outsider_client):
    """A fresh public repository of the outsider (created repositories outlive a test)."""
    name = f"mine-{uuid.uuid4().hex[:8]}"
    response = await outsider_client.post(
        "/api/repos/create", json={"type": "model", "name": name, "private": False}
    )
    assert response.status_code == 200, response.text
    return f"outsider/{name}"


def _commit_pointer(client, repo_id, oid, size, path="stolen.bin"):
    return client.post(
        f"/api/models/{repo_id}/commit/main",
        content=encode_ndjson(
            [
                {"key": "header", "value": {"summary": "link", "description": ""}},
                {
                    "key": "lfsFile",
                    "value": {"path": path, "oid": oid, "size": size, "algo": "sha256"},
                },
            ]
        ),
        headers=NDJSON,
    )


def _paths_of(repo_id):
    from kohakuhub.db import File, Repository

    namespace, name = repo_id.split("/")
    repo = Repository.get(
        (Repository.repo_type == "model")
        & (Repository.namespace == namespace)
        & (Repository.name == name)
    )
    return {row.path_in_repo for row in File.select().where(File.repository == repo)}


# --- the attack pattern ----------------------------------------------------


async def test_a_writer_cannot_attach_an_object_only_a_private_repo_holds(
    outsider_client, backend_test_state
):
    """The writer knows only the object id; no batch call, no upload."""
    secret = b"private dataset shard"
    oid = _store(backend_test_state, secret)
    _hold(_private_dataset(), oid, len(secret))
    repo_id = await _own_repo(outsider_client)

    response = await _commit_pointer(outsider_client, repo_id, oid, len(secret))

    # answered like an object stored nowhere, and nothing was linked
    assert response.status_code == 400, response.text
    assert f"LFS object {oid} not found in storage" in response.text
    assert "stolen.bin" not in _paths_of(repo_id)


async def test_the_refusal_is_the_same_as_for_an_object_stored_nowhere(
    outsider_client, backend_test_state
):
    secret = b"another private shard"
    held = _store(backend_test_state, secret)
    _hold(_private_dataset(), held, len(secret))
    nowhere = hashlib.sha256(b"never uploaded").hexdigest()
    repo_id = await _own_repo(outsider_client)

    first = await _commit_pointer(outsider_client, repo_id, held, len(secret))
    second = await _commit_pointer(outsider_client, repo_id, nowhere, 14)

    assert first.status_code == second.status_code == 400
    def shape(text, oid):
        return re.sub(r"lfs/\w\w/\w\w/OID", "KEY", text.replace(oid, "OID"))

    assert shape(first.text, held) == shape(second.text, nowhere)


# --- what stays allowed ----------------------------------------------------


async def test_a_reader_of_a_holding_repo_may_attach_the_object(owner_client, backend_test_state):
    """The owner can read the private dataset, so the content is theirs to link."""
    content = b"shared with the owner"
    oid = _store(backend_test_state, content)
    _hold(_private_dataset(), oid, len(content))

    response = await _commit_pointer(owner_client, "owner/demo-model", oid, len(content), "ok.bin")

    assert response.status_code == 200, response.text
    assert "ok.bin" in _paths_of("owner/demo-model")


async def test_an_object_held_by_a_public_repo_may_be_attached_by_anyone(
    outsider_client, backend_test_state
):
    content = b"public weights"
    oid = _store(backend_test_state, content)
    from kohakuhub.db import Repository

    public = Repository.get(
        (Repository.namespace == "owner") & (Repository.name == "demo-model")
    )
    _hold(public, oid, len(content))
    repo_id = await _own_repo(outsider_client)

    response = await _commit_pointer(outsider_client, repo_id, oid, len(content), "copy.bin")

    assert response.status_code == 200, response.text


async def test_an_object_the_repo_was_handed_an_upload_for_commits(
    outsider_client, backend_test_state
):
    content = b"uploaded by the writer"
    oid = _store(backend_test_state, content)
    repo_id = await _own_repo(outsider_client)
    grant_lfs_upload("outsider", repo_id.split("/")[1], oid)

    response = await _commit_pointer(outsider_client, repo_id, oid, len(content), "mine.bin")

    assert response.status_code == 200, response.text
