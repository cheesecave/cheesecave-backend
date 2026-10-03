"""The S3 maintenance scripts read an endpoint with a path as KohakuHub does (#133).

scripts/clear_s3_storage.py and scripts/show_s3_usage.py build their own
client: with ``--endpoint https://<host>/<real-bucket>`` their ``--bucket`` is a
key prefix inside the real bucket, so they list and delete what is really
stored there, and nothing outside it. Checked on the wire, as in
test_s3_bucket_in_endpoint.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from test.kohakuhub.utils.test_s3_bucket_in_endpoint import Wire

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def clear():
    return _load("clear_s3_storage")


@pytest.fixture(scope="module")
def usage():
    return _load("show_s3_usage")


LISTING = (
    b"<ListBucketResult><Name>real</Name><Prefix>hub-storage/</Prefix><IsTruncated>false</IsTruncated>"
    b"<Contents><Key>hub-storage/lfs/aa/bb/x</Key><Size>5</Size></Contents>"
    b"<Contents><Key>hub-storage/hf-model-demo/data/y</Key><Size>7</Size></Contents>"
    b"</ListBucketResult>"
)


def test_clearing_lists_and_deletes_inside_the_prefix(clear):
    s3 = clear.get_s3_client("https://s3.example.com/real", "ak", "sk", bucket="hub-storage")
    deleted = b"<DeleteResult><Deleted><Key>hub-storage/lfs/aa/bb/x</Key></Deleted></DeleteResult>"
    wire = Wire(s3, (200, LISTING), (200, deleted))
    keys, size = clear.list_objects(s3, "hub-storage", prefixes=["lfs/"])
    assert wire.path() == "/real" and wire.query()["prefix"] == ["hub-storage/lfs/"]
    assert keys == ["lfs/aa/bb/x", "hf-model-demo/data/y"] and size == 12
    count, errors = clear.delete_objects(s3, "hub-storage", ["lfs/aa/bb/x"])
    assert wire.path() == "/real" and b"<Key>hub-storage/lfs/aa/bb/x</Key>" in wire.last.body
    assert (count, errors) == (1, [])


def test_clearing_everything_still_stays_inside_the_prefix(clear):
    s3 = clear.get_s3_client("https://s3.example.com/real", "ak", "sk", bucket="hub-storage")
    wire = Wire(s3, (200, LISTING))
    clear.list_objects(s3, "hub-storage")
    assert wire.query()["prefix"] == ["hub-storage/"]


def test_a_plain_endpoint_is_used_as_it_is(clear, usage):
    for module in (clear, usage):
        s3 = module.get_s3_client("http://minio:9000", "ak", "sk", bucket="hub-storage")
        wire = Wire(s3, (200, b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"))
        s3.list_objects_v2(Bucket="hub-storage")
        assert s3.meta.endpoint_url == "http://minio:9000"
        assert wire.path() == "/hub-storage" and "prefix" not in wire.query()


def test_usage_counts_what_the_prefix_holds(usage):
    s3 = usage.get_s3_client("https://s3.example.com/real", "ak", "sk", bucket="hub-storage")
    wire = Wire(s3, (200, LISTING))
    stats = usage.analyze_storage(s3, "hub-storage")
    assert wire.path() == "/real" and wire.query()["prefix"] == ["hub-storage/"]
    assert (stats["lfs"]["count"], stats["lfs"]["size"]) == (1, 5)
    assert (stats["models"]["count"], stats["models"]["size"]) == (1, 7)
