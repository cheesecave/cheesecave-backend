"""Presigned URLs against a real MinIO, reached at two different addresses.

``test_s3_presign.py`` checks the signatures offline; here the URLs are used.
The API reaches the store at ``KOHAKU_HUB_S3_ENDPOINT`` and a client at
``KOHAKU_HUB_S3_PUBLIC_ENDPOINT``: the same server under two host names
(``127.0.0.1`` and ``localhost``), the way the default docker compose reaches
MinIO as ``minio:9000`` inside and ``127.0.0.1:29001`` outside. Every kind of
URL has to work in every combination of signature version, address and bucket
layout, and a multipart upload of more than ten parts (made in the process
pool) is the case that used to fail with ``SignatureDoesNotMatch``.
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, urlunsplit

import boto3
import httpx
import pytest

import kohakuhub.utils.s3 as s3_module

PART = 5 * 1024 * 1024  # the smallest part a store accepts, except the last


def other_host(endpoint: str) -> str:
    """The same server under another name."""
    parts = urlsplit(endpoint)
    names = {"127.0.0.1": "localhost", "localhost": "127.0.0.1"}
    if parts.hostname not in names:
        pytest.skip(f"the test store is not on a loopback address: {parts.hostname}")
    return urlunsplit((parts.scheme, parts.netloc.replace(parts.hostname, names[parts.hostname]), parts.path, "", ""))


@pytest.fixture(scope="module")
def store():
    """A real bucket on the configured MinIO, removed afterwards."""
    cfg = s3_module.cfg.s3
    admin = boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region,
    )
    bucket = f"presign-live-{uuid.uuid4().hex[:10]}"
    admin.create_bucket(Bucket=bucket)
    try:
        yield {"endpoint": cfg.endpoint, "bucket": bucket, "admin": admin}
    finally:
        for page in admin.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                admin.delete_object(Bucket=bucket, Key=item["Key"])
        admin.delete_bucket(Bucket=bucket)


@pytest.fixture(params=[None, "s3v4"], ids=["default-signing", "sigv4"])
def signature_version(request):
    return request.param


@pytest.fixture(params=["same-address", "other-host"])
def address(request):
    return request.param


@pytest.fixture(params=["bucket", "bucket-in-endpoint"])
def layout(request):
    return request.param


@pytest.fixture
def configured(monkeypatch, store, signature_version, address, layout):
    """The configuration of one combination; returns the object-name prefix to use."""
    endpoint = store["endpoint"]
    if layout == "bucket-in-endpoint":  # the real bucket is in the endpoint's path, ``cfg.s3.bucket`` a key prefix in it
        endpoint = f"{endpoint}/{store['bucket']}"
        name = f"prefix-{uuid.uuid4().hex[:6]}"
    else:
        name = store["bucket"]
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", endpoint)
    monkeypatch.setattr(s3_module.cfg.s3, "public_endpoint", endpoint if address == "same-address" else other_host(endpoint))
    monkeypatch.setattr(s3_module.cfg.s3, "bucket", name)
    monkeypatch.setattr(s3_module.cfg.s3, "signature_version", signature_version)
    monkeypatch.setattr(s3_module.cfg.s3, "force_path_style", True)
    return {"bucket": name, "prefix": uuid.uuid4().hex[:8]}


@pytest.fixture
def http():
    with httpx.Client(trust_env=False, timeout=120) as client:
        yield client


def test_a_presigned_upload_and_download_work_at_the_public_address(configured, http):
    key = f"{configured['prefix']}/small.bin"
    content = os.urandom(4096)

    upload = s3_module._generate_upload_presigned_url_sync(configured["bucket"], key)
    put = http.put(upload["url"], content=content)
    download = http.get(s3_module._generate_download_presigned_url_sync(configured["bucket"], key))

    assert put.status_code == 200, put.text
    assert download.status_code == 200, download.text
    assert download.content == content
    assert urlsplit(upload["url"]).netloc == urlsplit(s3_module.cfg.s3.public_endpoint).netloc


@pytest.mark.asyncio
@pytest.mark.parametrize("part_count", [2, 11], ids=["2-parts", "11-parts-in-the-process-pool"])
async def test_a_multipart_upload_works_at_the_public_address(configured, http, part_count):
    key = f"{configured['prefix']}/multipart.bin"
    sizes = [PART] * (part_count - 1) + [1234]
    parts_data = [os.urandom(size) for size in sizes]

    started = await s3_module.generate_multipart_upload_urls(configured["bucket"], key, part_count)
    etags = []
    for item, data in zip(started["part_urls"], parts_data):
        response = http.put(item["url"], content=data)
        assert response.status_code == 200, f"part {item['part_number']}: {response.text}"
        etags.append({"PartNumber": item["part_number"], "ETag": response.headers["etag"]})
    await s3_module.complete_multipart_upload(configured["bucket"], key, started["upload_id"], etags)

    downloaded = http.get(await s3_module.generate_download_presigned_url(configured["bucket"], key))
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(b"".join(parts_data)).hexdigest()


def test_the_store_refuses_a_url_signed_for_another_host(monkeypatch, store, http):
    """The reason all of the above matters: SigV4 signs the host the request carries."""
    endpoint = store["endpoint"]
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", endpoint)
    monkeypatch.setattr(s3_module.cfg.s3, "public_endpoint", endpoint)
    monkeypatch.setattr(s3_module.cfg.s3, "bucket", store["bucket"])
    monkeypatch.setattr(s3_module.cfg.s3, "signature_version", "s3v4")
    signed_for_endpoint = s3_module.get_s3_client().generate_presigned_url(
        "put_object", Params={"Bucket": store["bucket"], "Key": "wrong-host.bin"}, ExpiresIn=300
    )

    at_its_own_address = http.put(signed_for_endpoint, content=b"x")
    at_another_name = http.put(signed_for_endpoint.replace(endpoint, other_host(endpoint)), content=b"x")

    assert at_its_own_address.status_code == 200
    assert at_another_name.status_code == 403
    assert "SignatureDoesNotMatch" in at_another_name.text


class Proxy:
    """A reverse proxy in front of the store, on a path: ``/s3/...`` goes to the store as ``/...``.

    ``keep_host`` is nginx's ``proxy_set_header Host $http_host``; without it the request reaches the
    store with the upstream's own host name, nginx's default."""

    def __init__(self, upstream: str, keep_host: bool):
        proxy = self
        self.upstream, self.keep_host = urlsplit(upstream), keep_host

        class Handler(BaseHTTPRequestHandler):
            def do_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                host = self.headers["Host"] if proxy.keep_host else proxy.upstream.netloc
                target = f"{proxy.upstream.scheme}://{proxy.upstream.netloc}{self.path.removeprefix('/s3')}"
                answer = httpx.request(self.command, target, content=body, headers={"Host": host}, trust_env=False)
                self.send_response(answer.status_code)
                self.send_header("Content-Length", str(len(answer.content)))
                self.end_headers()
                self.wfile.write(answer.content)

            do_GET = do_PUT = do_request

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://localhost:{self.server.server_port}/s3"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("keep_host", [True, False], ids=["host-kept", "host-rewritten-to-the-upstream"])
def test_a_proxy_mounted_on_a_path_works_when_it_keeps_the_host(monkeypatch, store, http, keep_host):
    """The documented requirement: pass ``Host`` through (the mount path itself is not signed)."""
    endpoint = store["endpoint"]
    monkeypatch.setattr(s3_module.cfg.s3, "endpoint", endpoint)
    monkeypatch.setattr(s3_module.cfg.s3, "bucket", store["bucket"])
    monkeypatch.setattr(s3_module.cfg.s3, "signature_version", "s3v4")
    monkeypatch.setattr(s3_module.cfg.s3, "force_path_style", True)
    content = os.urandom(2048)

    with Proxy(endpoint, keep_host) as proxy:
        monkeypatch.setattr(s3_module.cfg.s3, "public_endpoint", proxy.url)
        key = f"proxied-{keep_host}.bin"
        put = http.put(s3_module._generate_upload_presigned_url_sync(store["bucket"], key)["url"], content=content)
        got = http.get(s3_module._generate_download_presigned_url_sync(store["bucket"], key))

    if keep_host:
        assert (put.status_code, got.status_code, got.content) == (200, 200, content)
    else:
        assert put.status_code == 403 and "SignatureDoesNotMatch" in put.text
