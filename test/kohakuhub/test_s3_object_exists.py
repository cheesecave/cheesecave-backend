"""`object_exists` tells a missing object from a store that cannot answer (#10).

It read every failure as "does not exist": a permission error or an outage on the store made the LFS verify answer
404, a commit answer 400 "not found" and a recoverability check call files missing, so clients re-uploaded or gave up
for what was no fault of theirs. Only the store saying "not found" is a missing object now; anything else raises, and
the routes that ask answer a server error, which clients retry.

Real MinIO for what is stored and what is not. The failures are injected (targeted mocks: a store that answers 403 or
is unreachable cannot be produced against the real service).
"""

import hashlib
import importlib

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from test.kohakuhub.test_lfs_gc import _age_recent, _commit, _lfs_op, _put  # noqa: F401  (helpers: real objects and commits)
from test.kohakuhub.test_lfs_gc import m  # noqa: F401  (the shared fixture)


def _s3():
    return importlib.import_module("kohakuhub.utils.s3")


def _bucket():
    return importlib.import_module("kohakuhub.config").cfg.s3.bucket


class _Store:
    """A store whose HEAD fails with ``error``."""

    def __init__(self, error):
        self.error = error

    def head_object(self, **kwargs):
        raise self.error


def _client_error(code, status=None):
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status or 0}}, "HeadObject")


async def test_a_stored_object_exists_and_a_missing_one_does_not(m):
    client = _s3().get_s3_client()
    client.put_object(Bucket=_bucket(), Key="t10/present.txt", Body=b"x")
    try:
        assert await _s3().object_exists(_bucket(), "t10/present.txt") is True
        assert await _s3().object_exists(_bucket(), "t10/absent.txt") is False
    finally:
        client.delete_object(Bucket=_bucket(), Key="t10/present.txt")


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_each_way_a_store_says_not_found_is_a_missing_object(m, monkeypatch, code):
    monkeypatch.setattr(_s3(), "get_s3_client", lambda: _Store(_client_error(code)))
    assert await _s3().object_exists(_bucket(), "any") is False


@pytest.mark.parametrize(
    "error",
    [
        _client_error("403", 403),
        _client_error("AccessDenied", 403),
        _client_error("500", 500),
        _client_error("503", 503),
        _client_error("SlowDown", 503),
        EndpointConnectionError(endpoint_url="http://store.invalid"),
        ConnectTimeoutError(endpoint_url="http://store.invalid"),
        ReadTimeoutError(endpoint_url="http://store.invalid"),
    ],
    ids=["403", "AccessDenied", "500", "503", "SlowDown", "unreachable", "connect timeout", "read timeout"],
)
async def test_a_store_that_cannot_answer_is_not_a_missing_object(m, monkeypatch, error):
    monkeypatch.setattr(_s3(), "get_s3_client", lambda: _Store(error))
    with pytest.raises(type(error)):
        await _s3().object_exists(_bucket(), "any")


# ----- what the routes that ask answer when the store cannot -----


async def test_a_commit_of_an_lfs_file_answers_a_server_error_not_missing_content(m, owner_client, monkeypatch):
    content = b"commit during an outage " * 30
    oid = _put(m, content)
    monkeypatch.setattr(_s3(), "get_s3_client", lambda: _Store(_client_error("403", 403)))
    response = await _commit(owner_client, "demo-model", _lfs_op(content, path="t10/outage.bin"))
    assert response.status_code == 500, response.text
    assert "Failed to verify LFS object in S3" in response.text  # not "LFS object ... not found in storage" (400)
    monkeypatch.undo()
    assert (await _commit(owner_client, "demo-model", _lfs_op(content, path="t10/outage.bin"))).status_code == 200
    m.db.File.delete().where(m.db.File.path_in_repo == "t10/outage.bin").execute()
    m.db.LFSObjectHistory.delete().where(m.db.LFSObjectHistory.path_in_repo == "t10/outage.bin").execute()


async def test_the_lfs_verify_answers_a_server_error_not_404_when_the_store_cannot_say(m, owner_client, monkeypatch):
    content = b"verify during an outage " * 30
    oid = _put(m, content)
    verify = "/api/owner/demo-model.git/info/lfs/verify"
    body = {"oid": oid, "size": len(content)}
    assert (await owner_client.post(verify, json=body)).status_code == 200
    monkeypatch.setattr(_s3(), "get_s3_client", lambda: _Store(_client_error("403", 403)))
    response = await owner_client.post(verify, json=body)
    assert response.status_code >= 500, (response.status_code, response.text)
    monkeypatch.undo()
    assert hashlib.sha256(content).hexdigest() == oid
