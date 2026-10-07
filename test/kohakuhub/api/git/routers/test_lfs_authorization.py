"""Authorization of the Git LFS routes, driven through the real application.

Roles come from the shared baseline: ``owner`` (owns the public model
``owner/demo-model`` and the organization ``acme-labs``), ``member`` (admin of
``acme-labs``), ``visitor`` (read-only in ``acme-labs``), ``outsider`` (no
access), and an anonymous caller. ``acme-labs/private-dataset`` is private.

What the tests pin:

* the batch route resolves the repository first. A repository that does not
  exist, or one the caller may not see, is handled exactly like an
  unauthorized request: ``401`` plus an ``LFS-Authenticate`` challenge for an
  anonymous caller, ``404`` for a signed-in one. No upload action is ever
  produced for a repository the caller is not allowed to write to;
* ``complete`` and ``verify`` accept either a signed ticket that the batch
  route put into the URL it handed out, or a signed-in caller with write
  permission on the repository named in the URL. Anything else gets ``401``;
* the end-to-end upload flows (single part and multipart) still work with the
  tickets, with clients that send no credentials on those two calls
  (``huggingface_hub`` sends none on ``complete``; the web UI sends none on
  either).
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest

LFS_HEADERS = {
    "Accept": "application/vnd.git-lfs+json",
    "Content-Type": "application/vnd.git-lfs+json",
}

OID = "a" * 64
OTHER_OID = "b" * 64

# repo key -> (batch base path, follow-up base path)
PUBLIC = ("/owner/demo-model.git", "/api/owner/demo-model.git/info/lfs")
PRIVATE = (
    "/datasets/acme-labs/private-dataset.git",
    "/api/acme-labs/private-dataset.git/info/lfs",
)
MISSING = ("/owner/no-such-repo.git", "/api/owner/no-such-repo.git/info/lfs")

REPOS = {"public": PUBLIC, "private": PRIVATE, "missing": MISSING}


@asynccontextmanager
async def _anonymous(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as anon:
        yield anon


async def _batch(client, repo, operation, *, oid=OID, size=3):
    return await client.post(
        f"{REPOS[repo][0]}/info/lfs/objects/batch",
        json={
            "operation": operation,
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": size}],
        },
        headers=LFS_HEADERS,
    )


def _challenge(response):
    value = response.headers.get("lfs-authenticate", "")
    assert value.startswith("Basic"), response.headers
    return value


def _ticket_module():
    # Imported late: the backend fixtures reload the kohakuhub modules, and the
    # ticket must come from the copy the running app uses.
    from kohakuhub.api.git.routers import lfs

    return lfs


def _path_and_query(href: str) -> str:
    parts = urlsplit(href)
    return parts.path + (f"?{parts.query}" if parts.query else "")


# ---------------------------------------------------------------------------
# batch: anonymous callers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation, repo",
    [
        ("upload", "public"),
        ("upload", "private"),
        ("upload", "missing"),
        ("download", "private"),
        ("download", "missing"),
    ],
)
async def test_batch_anonymous_is_challenged_and_gets_no_action(client, operation, repo):
    response = await _batch(client, repo, operation)

    assert response.status_code == 401
    _challenge(response)
    assert "actions" not in response.text


async def test_batch_download_of_a_public_repo_stays_anonymous(client):
    response = await _batch(client, "public", "download")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.git-lfs+json")


# ---------------------------------------------------------------------------
# batch: signed-in callers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation, repo, status",
    [
        ("download", "private", 404),  # cannot see it
        ("upload", "private", 404),
        ("download", "missing", 404),
        ("upload", "missing", 404),  # same answer as a private repo
        ("upload", "public", 403),  # can read, cannot write
        ("download", "public", 200),
    ],
)
async def test_batch_outsider_cannot_see_or_write(outsider_client, operation, repo, status):
    response = await _batch(outsider_client, repo, operation)

    assert response.status_code == status
    if status == 404:
        assert response.headers.get("x-error-code") == "RepoNotFound"
    assert "actions" not in response.text


async def test_batch_visitor_can_read_but_not_write_a_private_repo(visitor_client):
    download = await _batch(visitor_client, "private", "download")
    upload = await _batch(visitor_client, "private", "upload")

    assert download.status_code == 200
    assert upload.status_code == 403
    assert "actions" not in upload.text


async def test_batch_owner_gets_nothing_for_a_missing_repo(owner_client):
    for operation in ("upload", "download"):
        response = await _batch(owner_client, "missing", operation)
        assert response.status_code == 404, operation
        assert "actions" not in response.text


async def test_batch_writers_get_upload_actions_with_tickets(owner_client):
    response = await _batch(owner_client, "public", "upload")

    assert response.status_code == 200
    actions = response.json()["objects"][0]["actions"]
    assert actions["upload"]["href"]
    assert "ticket=" in actions["verify"]["href"]


async def test_batch_org_admin_can_upload_to_a_private_repo(member_client):
    response = await _batch(member_client, "private", "upload")

    assert response.status_code == 200
    assert "ticket=" in response.json()["objects"][0]["actions"]["verify"]["href"]


# ---------------------------------------------------------------------------
# verify / complete: no ticket, no valid session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["verify", "complete"])
@pytest.mark.parametrize("repo", ["public", "private", "missing"])
async def test_followup_without_credentials_is_challenged(client, route, repo):
    response = await client.post(f"{REPOS[repo][1]}/{route}", json={})

    assert response.status_code == 401
    _challenge(response)


@pytest.mark.parametrize(
    "route, caller, repo, status",
    [
        ("verify", "outsider_client", "private", 404),
        ("verify", "outsider_client", "missing", 404),
        ("verify", "outsider_client", "public", 403),
        ("verify", "visitor_client", "private", 403),
        ("verify", "owner_client", "missing", 404),
        ("complete", "outsider_client", "private", 404),
        ("complete", "outsider_client", "public", 403),
        ("complete", "visitor_client", "private", 403),
        ("complete", "owner_client", "missing", 404),
    ],
)
async def test_followup_signed_in_without_write_permission(
    owner_client, outsider_client, visitor_client, route, caller, repo, status
):
    client = {
        "owner_client": owner_client,
        "outsider_client": outsider_client,
        "visitor_client": visitor_client,
    }[caller]
    body = {"oid": OID, "size": 1, "upload_id": "u1", "parts": [{"partNumber": 1, "etag": "e"}]}

    response = await client.post(f"{REPOS[repo][1]}/{route}", json=body)

    assert response.status_code == status


async def test_followup_signed_in_writer_reaches_the_storage_check(owner_client):
    response = await owner_client.post(
        f"{PUBLIC[1]}/verify", json={"oid": OID, "size": 1}
    )

    # authorized, then the object simply is not there
    assert response.status_code == 404
    assert "Object not found" in response.text


# ---------------------------------------------------------------------------
# verify / complete: tickets
# ---------------------------------------------------------------------------


def _bad_tickets(lfs):
    return {
        "garbage": "garbage",
        "unsigned": "9999999999.deadbeef",
        "wrong repo": lfs.issue_lfs_ticket("verify", "owner/other", OID),
        "wrong oid": lfs.issue_lfs_ticket("verify", "owner/demo-model", OTHER_OID),
        "wrong purpose": lfs.issue_lfs_ticket("complete", "owner/demo-model", OID),
        "expired": lfs.issue_lfs_ticket("verify", "owner/demo-model", OID, ttl=-10),
    }


async def test_verify_rejects_every_bad_ticket(client):
    lfs = _ticket_module()
    for label, ticket in _bad_tickets(lfs).items():
        response = await client.post(
            f"{PUBLIC[1]}/verify",
            params={"ticket": ticket},
            json={"oid": OID, "size": 1},
        )
        assert response.status_code == 401, label


async def test_verify_with_a_valid_ticket_reaches_the_storage_check(client):
    lfs = _ticket_module()
    ticket = lfs.issue_lfs_ticket("verify", "owner/demo-model", OID)

    response = await client.post(
        f"{PUBLIC[1]}/verify", params={"ticket": ticket}, json={"oid": OID, "size": 1}
    )

    assert response.status_code == 404
    assert "Object not found" in response.text


async def test_verify_ticket_does_not_cover_completing_a_multipart_upload(client):
    lfs = _ticket_module()
    ticket = lfs.issue_lfs_ticket("verify", "owner/demo-model", OID)

    response = await client.post(
        f"{PUBLIC[1]}/verify",
        params={"ticket": ticket},
        json={
            "oid": OID,
            "size": 1,
            "upload_id": "u1",
            "parts": [{"PartNumber": 1, "ETag": "e"}],
        },
    )

    assert response.status_code == 401


async def test_complete_rejects_every_bad_ticket(client):
    lfs = _ticket_module()
    body = {"oid": OID, "upload_id": "u1", "parts": [{"partNumber": 1, "etag": "e"}]}
    bad = {
        "garbage": "garbage",
        "wrong repo": lfs.issue_lfs_ticket("complete", "owner/other", OID, "u1"),
        "wrong oid": lfs.issue_lfs_ticket("complete", "owner/demo-model", OTHER_OID, "u1"),
        "wrong upload": lfs.issue_lfs_ticket("complete", "owner/demo-model", OID, "u2"),
        "wrong purpose": lfs.issue_lfs_ticket("verify", "owner/demo-model", OID, "u1"),
        "expired": lfs.issue_lfs_ticket("complete", "owner/demo-model", OID, "u1", ttl=-10),
    }
    for label, ticket in bad.items():
        response = await client.post(
            f"{PUBLIC[1]}/complete/u1", params={"ticket": ticket}, json=body
        )
        assert response.status_code == 401, label


async def test_complete_with_a_valid_ticket_reaches_the_storage(client):
    lfs = _ticket_module()
    ticket = lfs.issue_lfs_ticket("complete", "owner/demo-model", OID, "no-such-upload")

    response = await client.post(
        f"{PUBLIC[1]}/complete/no-such-upload",
        params={"ticket": ticket},
        json={"oid": OID, "parts": [{"partNumber": 1, "etag": "e"}]},
    )

    # authorized; the storage then refuses an upload id that never existed
    assert response.status_code == 500


async def test_oids_must_be_sha256_hex(owner_client):
    batch = await _batch(owner_client, "public", "upload", oid="../../etc/passwd")
    verify = await owner_client.post(f"{PUBLIC[1]}/verify", json={"oid": "../x", "size": 1})

    assert batch.status_code == 200
    assert batch.json()["objects"][0]["error"]["code"] == 422
    assert "actions" not in batch.text
    assert verify.status_code == 400


# ---------------------------------------------------------------------------
# end to end: the flows real clients use
# ---------------------------------------------------------------------------


async def test_single_part_upload_flow_with_tickets(app, owner_client):
    payload = b"lfs single part payload"
    oid = hashlib.sha256(payload).hexdigest()

    batch = await _batch(owner_client, "public", "upload", oid=oid, size=len(payload))
    assert batch.status_code == 200
    actions = batch.json()["objects"][0]["actions"]

    async with httpx.AsyncClient() as raw:
        put = await raw.put(
            actions["upload"]["href"], content=payload, headers=actions["upload"].get("header", {})
        )
    assert put.status_code == 200, put.text

    verify_url = _path_and_query(actions["verify"]["href"])
    async with _anonymous(app) as anon:
        ok = await anon.post(verify_url, json={"oid": oid, "size": len(payload)})
        wrong_oid = await anon.post(verify_url, json={"oid": OTHER_OID, "size": len(payload)})

    assert ok.status_code == 200
    assert ok.json()["message"] == "Object verified successfully"
    assert wrong_oid.status_code == 401


async def test_multipart_upload_flow_with_tickets(app, owner_client):
    from kohakuhub.config import cfg

    chunk = 5 * 1024 * 1024
    payload = (b"m" * chunk) + (b"n" * 1024)
    oid = hashlib.sha256(payload).hexdigest()
    cfg.app.lfs_multipart_threshold_bytes = 1
    cfg.app.lfs_multipart_chunk_size_bytes = chunk
    try:
        batch = await _batch(owner_client, "public", "upload", oid=oid, size=len(payload))
    finally:
        cfg.app.lfs_multipart_threshold_bytes = 100 * 1024 * 1024
        cfg.app.lfs_multipart_chunk_size_bytes = 50 * 1024 * 1024
    assert batch.status_code == 200
    actions = batch.json()["objects"][0]["actions"]
    header = actions["upload"]["header"]
    assert header["chunk_size"] == str(chunk)

    parts = []
    async with httpx.AsyncClient(timeout=120) as raw:
        for number, piece in ((1, payload[:chunk]), (2, payload[chunk:])):
            put = await raw.put(header[str(number)], content=piece)
            assert put.status_code == 200, put.text
            parts.append({"partNumber": number, "etag": put.headers["etag"]})

    complete_url = _path_and_query(actions["upload"]["href"])
    assert "ticket=" in complete_url
    async with _anonymous(app) as anon:
        done = await anon.post(complete_url, json={"oid": oid, "size": len(payload), "parts": parts})
        verified = await anon.post(
            _path_and_query(actions["verify"]["href"]),
            json={"oid": oid, "size": len(payload)},
        )

    assert done.status_code == 200, done.text
    assert verified.status_code == 200
