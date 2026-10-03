"""The real bucket in the S3 endpoint's path, on the real stack (#133).

Runs where ``KOHAKU_HUB_S3_ENDPOINT`` carries a path, as in the CI job
"backend tests (S3 endpoint with a path)"; skipped elsewhere. KohakuHub's own
client readdresses the configured bucket, so these tests ask the real bucket
directly, through a client of the root endpoint that does not: what is really
stored, not what the client reports.
"""

import hashlib
import json
import os
from urllib.parse import urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from kohakuhub.task_testing import RecordingContext
from test.kohakuhub.api.commit.test_availability import Repo, _file, _live, lfs, m  # noqa: F401

pytestmark = pytest.mark.skipif(
    not urlparse(os.environ.get("KOHAKU_HUB_S3_ENDPOINT", "")).path.strip("/"),
    reason="needs an S3 endpoint with the bucket in its path",
)


@pytest.fixture
def real(m):  # noqa: F811 - the fixture imported above
    """``(client of the root endpoint, real bucket, key prefix)``."""
    root, bucket, prefix = _live("kohakuhub.utils.s3").bucket_in_endpoint()
    client = boto3.client(
        "s3",
        endpoint_url=root,
        aws_access_key_id=m.cfg.s3.access_key,
        aws_secret_access_key=m.cfg.s3.secret_key,
        region_name=m.cfg.s3.region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return client, bucket, prefix


def _keys(real, under):
    client, bucket, prefix = real
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix + under)
    return [o["Key"][len(prefix) :] for page in pages for o in page.get("Contents", [])]


def _stored(real, key):
    client, bucket, prefix = real
    try:
        client.head_object(Bucket=bucket, Key=prefix + key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


async def test_objects_live_under_the_prefix_and_nothing_outside_it(m, real, owner_client):  # noqa: F811
    repo = await Repo(m, owner_client, "path-layout").create()
    await repo.commit(_file("notes.txt", "notes"), lfs("weights.bin", b"weights"))
    assert any(key.startswith(f"{repo.lakefs_repo}/data/") for key in _keys(real, f"{repo.lakefs_repo}/"))
    assert _stored(real, m.gc.lfs_key(hashlib.sha256(b"weights").hexdigest()))
    # KohakuHub's own client agrees, with the prefix taken off
    listed = m.s3.list_objects_v2(Bucket=m.cfg.s3.bucket, Prefix=f"{repo.lakefs_repo}/")
    assert {o["Key"] for o in listed["Contents"]} == set(_keys(real, f"{repo.lakefs_repo}/"))

    # The startup check finds the real bucket: it writes nothing at its root,
    # where it once created an object named after the configured bucket. (The
    # root may hold other data: the documented case of a shared bucket.)
    _live("kohakuhub.utils.s3").init_storage()
    client, bucket, prefix = real
    root = client.list_objects_v2(Bucket=bucket, Delimiter="/")
    assert prefix.split("/")[0] not in {o["Key"] for o in root.get("Contents", [])}
    assert prefix.split("/")[0] + "/" in {p["Prefix"] for p in root.get("CommonPrefixes", [])}


async def test_deleting_a_repository_deletes_its_objects(m, real, owner_client):  # noqa: F811
    repo = await Repo(m, owner_client, "path-purge").create()
    await repo.commit(_file("notes.txt", "notes"), _file("more.txt", "more"))
    lakefs_repo = repo.lakefs_repo
    assert _keys(real, f"{lakefs_repo}/")
    response = await owner_client.request(
        "DELETE", "/api/repos/delete", json={"type": "model", "name": repo.name}
    )
    assert response.status_code == 200, response.text
    cleanup = _live("kohakuhub.storage_cleanup")
    T = m.db.BackgroundTask
    task = T.get((T.kind == cleanup.PURGE_KIND) & T.payload.contains(lakefs_repo))
    context = RecordingContext()
    await cleanup.purge_repository(json.loads(task.payload), context)
    assert _keys(real, f"{lakefs_repo}/") == []


def test_collection_deletes_what_it_reports_deleted(m, real):  # noqa: F811
    cleanup = _live("kohakuhub.storage_cleanup")
    keys = [f"path-gc/{name}" for name in ("a", "b", "c")]
    for key in keys:
        m.s3.put_object(Bucket=m.cfg.s3.bucket, Key=key, Body=b"x")
    assert all(_stored(real, key) for key in keys)
    cleanup._delete_keys(m.cfg.s3.bucket, keys[:1])
    assert [_stored(real, key) for key in keys] == [False, True, True]
    assert cleanup._delete_prefix_batch(m.cfg.s3.bucket, "path-gc/") == 2
    assert _keys(real, "path-gc/") == []
