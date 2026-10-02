"""An S3 endpoint carrying the real bucket in its path (#133).

``https://<account>.r2.cloudflarestorage.com/<bucket>`` with ``s3.bucket =
hub-storage``: the configured bucket is a key prefix inside the real one.
These tests look at what goes on the wire (``before-send``), so they pin the
URLs, headers and bodies S3 really receives, and feed back canned answers.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.awsrequest import AWSResponse

import kohakuhub.utils.s3 as s3_module


class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def stream(self, **kwargs):
        yield self.body


class Wire:
    """Records every request a client sends and answers it from a queue."""

    def __init__(self, client, *answers):
        self.sent, self.answers = [], list(answers)
        client.meta.events.register("before-send.s3", self)

    def __call__(self, request, **kwargs):
        self.sent.append(request)
        status, body = self.answers.pop(0) if self.answers else (200, b"")
        return AWSResponse(request.url, status, {}, _Raw(body))

    @property
    def last(self):
        return self.sent[-1]

    def path(self, n=-1):
        return urlsplit(self.sent[n].url).path

    def query(self, n=-1):
        return parse_qs(urlsplit(self.sent[n].url).query, keep_blank_values=True)


@pytest.fixture(autouse=True)
def _bucket_in_endpoint(monkeypatch):
    cfg = s3_module.cfg.s3
    monkeypatch.setattr(cfg, "endpoint", "https://s3.example.com/real")
    monkeypatch.setattr(cfg, "public_endpoint", "https://s3.example.com/real")
    monkeypatch.setattr(cfg, "bucket", "hub-storage")
    monkeypatch.setattr(cfg, "access_key", "ak")
    monkeypatch.setattr(cfg, "secret_key", "sk")
    monkeypatch.setattr(cfg, "region", "us-east-1")
    monkeypatch.setattr(cfg, "signature_version", "s3v4")
    monkeypatch.setattr(cfg, "force_path_style", True)


def test_the_layout_is_read_from_the_endpoint(monkeypatch):
    assert s3_module.bucket_in_endpoint() == ("https://s3.example.com", "real", "hub-storage/")
    # Any further path is part of the prefix, before the configured bucket
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", "https://s3.example.com/real/team/")
    assert s3_module.bucket_in_endpoint() == ("https://s3.example.com", "real", "team/hub-storage/")
    for plain in ("http://minio:9000", "http://minio:9000/", ""):
        monkeypatch.setattr(s3_module.cfg.s3, "endpoint", plain)
        assert s3_module.bucket_in_endpoint() is None


def test_object_requests_reach_the_prefixed_key_in_the_real_bucket():
    s3 = s3_module.get_s3_client()
    wire = Wire(s3)
    assert s3.meta.endpoint_url == "https://s3.example.com"
    s3.put_object(Bucket="hub-storage", Key="lfs/ab/cd/x", Body=b"x")
    s3.get_object(Bucket="hub-storage", Key="lfs/ab/cd/x")
    s3.head_object(Bucket="hub-storage", Key="lfs/ab/cd/x")
    s3.delete_object(Bucket="hub-storage", Key="lfs/ab/cd/x")
    assert [request.method for request in wire.sent] == ["PUT", "GET", "HEAD", "DELETE"]
    assert {wire.path(n) for n in range(4)} == {"/real/hub-storage/lfs/ab/cd/x"}


def test_a_copy_names_its_source_in_the_real_bucket():
    """CopyObject's source travels in a header, never through the endpoint
    path: without this, S3 looks for a bucket named after the prefix."""
    s3 = s3_module.get_s3_client()
    ok = (200, b"<CopyObjectResult><ETag>&quot;e&quot;</ETag></CopyObjectResult>")
    wire = Wire(s3, ok, ok, ok, ok)
    s3.copy_object(Bucket="hub-storage", Key="b", CopySource={"Bucket": "hub-storage", "Key": "a/x y"})
    assert wire.path() == "/real/hub-storage/b"
    assert wire.last.headers["x-amz-copy-source"] == b"real/hub-storage/a/x%20y"
    s3.copy_object(Bucket="hub-storage", Key="b", CopySource="hub-storage/a?versionId=1")
    assert wire.last.headers["x-amz-copy-source"] == b"real/hub-storage/a?versionId=1"
    # A source in another bucket is left as it is, in either form
    s3.copy_object(Bucket="hub-storage", Key="b", CopySource={"Bucket": "other", "Key": "a"})
    assert wire.last.headers["x-amz-copy-source"] == b"other/a"
    s3.copy_object(Bucket="hub-storage", Key="b", CopySource="/other/a")
    assert wire.last.headers["x-amz-copy-source"] == b"/other/a"


def test_a_copy_source_quotes_a_prefix_needing_it(monkeypatch):
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", "https://s3.example.com/real/team a")
    s3 = s3_module.get_s3_client()
    wire = Wire(s3, (200, b"<CopyObjectResult><ETag>&quot;e&quot;</ETag></CopyObjectResult>"))
    s3.copy_object(Bucket="hub-storage", Key="b", CopySource={"Bucket": "hub-storage", "Key": "a"})
    assert wire.path() == "/real/team%20a/hub-storage/b"
    assert wire.last.headers["x-amz-copy-source"] == b"real/team%20a/hub-storage/a"


def test_listings_stay_inside_the_prefix_and_answer_relative_keys():
    s3 = s3_module.get_s3_client()
    page = (
        b"<ListBucketResult><Name>real</Name><Prefix>hub-storage/lfs/</Prefix>"
        b"<StartAfter>hub-storage/lfs/a</StartAfter><KeyCount>2</KeyCount><MaxKeys>2</MaxKeys>"
        b"<IsTruncated>false</IsTruncated>"
        b"<Contents><Key>hub-storage/lfs/b</Key><Size>1</Size></Contents>"
        b"<Contents><Key>hub-storage/lfs/c</Key><Size>2</Size></Contents>"
        b"<CommonPrefixes><Prefix>hub-storage/lfs/d/</Prefix></CommonPrefixes>"
        b"</ListBucketResult>"
    )
    wire = Wire(s3, (200, page), (200, b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"))
    listed = s3.list_objects_v2(Bucket="hub-storage", Prefix="lfs/", StartAfter="lfs/a", Delimiter="/")
    assert wire.path() == "/real"
    assert wire.query()["prefix"] == ["hub-storage/lfs/"]
    assert wire.query()["start-after"] == ["hub-storage/lfs/a"]
    assert [o["Key"] for o in listed["Contents"]] == ["lfs/b", "lfs/c"]
    assert listed["CommonPrefixes"] == [{"Prefix": "lfs/d/"}]
    assert (listed["Name"], listed["Prefix"], listed["StartAfter"]) == ("hub-storage", "lfs/", "lfs/a")
    # No prefix asked for: still never the whole real bucket
    s3.list_objects_v2(Bucket="hub-storage")
    assert wire.query()["prefix"] == ["hub-storage/"]


def test_url_encoded_listings_are_decoded_before_the_prefix_goes():
    """botocore asks for url-encoded keys and decodes them after the call; the
    prefix comes off after that, so keys with spaces come back whole."""
    s3 = s3_module.get_s3_client()
    page = (
        b"<ListBucketResult><EncodingType>url</EncodingType><Prefix>hub-storage/d%20e/</Prefix>"
        b"<Contents><Key>hub-storage/d%20e/f%20g</Key><Size>1</Size></Contents></ListBucketResult>"
    )
    wire = Wire(s3, (200, page))
    listed = s3.list_objects_v2(Bucket="hub-storage", Prefix="d e/")
    assert wire.query()["encoding-type"] == ["url"]
    assert wire.query()["prefix"] == ["hub-storage/d e/"]
    assert listed["Contents"][0]["Key"] == "d e/f g" and listed["Prefix"] == "d e/"


def test_a_bulk_delete_names_prefixed_keys():
    s3 = s3_module.get_s3_client()
    answer = (
        b"<DeleteResult><Deleted><Key>hub-storage/a</Key></Deleted>"
        b"<Error><Key>hub-storage/b</Key><Code>AccessDenied</Code><Message>no</Message></Error>"
        b"</DeleteResult>"
    )
    wire = Wire(s3, (200, answer))
    result = s3.delete_objects(
        Bucket="hub-storage", Delete={"Objects": [{"Key": "a"}, {"Key": "b"}], "Quiet": False}
    )
    assert wire.path() == "/real" and "delete" in wire.query()
    body = wire.last.body.decode() if isinstance(wire.last.body, bytes) else wire.last.body.read().decode()
    assert "<Key>hub-storage/a</Key>" in body and "<Key>hub-storage/b</Key>" in body
    assert result["Deleted"] == [{"Key": "a"}]
    assert result["Errors"][0]["Key"] == "b"


def test_bucket_requests_reach_the_real_bucket():
    s3 = s3_module.get_s3_client()
    wire = Wire(s3)
    s3.head_bucket(Bucket="hub-storage")
    s3.create_bucket(Bucket="hub-storage")
    # As init_storage asks outside us-east-1 (R2's region is "auto")
    s3.create_bucket(Bucket="hub-storage", CreateBucketConfiguration={"LocationConstraint": "auto"})
    assert [(r.method, wire.path(n)) for n, r in enumerate(wire.sent)] == [
        ("HEAD", "/real"),
        ("PUT", "/real"),
        ("PUT", "/real"),
    ]
    assert b"<LocationConstraint>auto</LocationConstraint>" in wire.last.body


def test_multipart_uploads_and_presigned_urls_use_the_prefixed_key():
    s3 = s3_module.get_s3_client()
    started = (
        b"<InitiateMultipartUploadResult><Bucket>real</Bucket><Key>hub-storage/big</Key>"
        b"<UploadId>u1</UploadId></InitiateMultipartUploadResult>"
    )
    wire = Wire(s3, (200, started))
    upload = s3.create_multipart_upload(Bucket="hub-storage", Key="big")
    assert wire.path() == "/real/hub-storage/big"
    assert (upload["Bucket"], upload["Key"], upload["UploadId"]) == ("hub-storage", "big", "u1")
    url = s3.generate_presigned_url(
        "upload_part",
        Params={"Bucket": "hub-storage", "Key": "big", "UploadId": "u1", "PartNumber": 1},
    )
    assert urlsplit(url).path == "/real/hub-storage/big"
    assert url.startswith("https://s3.example.com/real/")  # what the public-endpoint rewrite matches


def test_other_buckets_and_plain_endpoints_are_left_alone(monkeypatch):
    s3 = s3_module.get_s3_client()
    wire = Wire(s3)
    s3.get_object(Bucket="other", Key="x")
    assert wire.path() == "/other/x"
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", "http://minio:9000")
    s3 = s3_module.get_s3_client()
    listing = b"<ListBucketResult><Contents><Key>a</Key></Contents></ListBucketResult>"
    wire = Wire(s3, (200, b""), (200, listing))
    s3.put_object(Bucket="hub-storage", Key="a", Body=b"x")
    assert wire.path() == "/hub-storage/a"
    listed = s3.list_objects_v2(Bucket="hub-storage")
    assert "prefix" not in wire.query() and listed["Contents"][0]["Key"] == "a"


def test_download_urls_move_to_the_public_endpoint_with_the_prefixed_path(monkeypatch):
    """Presigned URLs come from the root endpoint and still start with the
    internal endpoint (path included), which the public one replaces."""
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", "http://minio:9000/real")
    monkeypatch.setattr(s3_module.cfg.s3, "public_endpoint", "https://files.example.com/real")
    url = s3_module._generate_download_presigned_url_sync("hub-storage", "lfs/ab/cd/x", 60, "x.bin")
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "files.example.com", "/real/hub-storage/lfs/ab/cd/x")
