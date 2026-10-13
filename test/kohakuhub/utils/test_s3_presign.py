"""Presigned URLs are signed for the address clients use, whatever the part count.

An object store checks the signature against the ``Host`` the request arrives
with (SigV4 signs it: ``X-Amz-SignedHeaders=host``). The API reaches the store
at ``KOHAKU_HUB_S3_ENDPOINT`` and clients at ``KOHAKU_HUB_S3_PUBLIC_ENDPOINT``,
so every URL handed out has to be signed for the public address. These tests
build the URLs with the real botocore (signing is local, no network), recompute
the SigV4 signature independently from the AWS specification, and compare the
signature algorithm of every way Cheesecave builds a URL: download, single
upload, and a multipart part from the sequential and from the process-pool path
(``test_s3_presign_live.py`` sends them to a real MinIO).
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qsl, quote, urlsplit

import pytest

import kohakuhub.utils.s3 as s3_module

ACCESS_KEY, SECRET_KEY, BUCKET = "access-key", "secret-key", "hub-storage"
OBJECT_KEY = "lfs/ab/cd/abcdef"

# name: (endpoint, public endpoint, signature version, host the signature must cover,
#        path prefix the client puts in front of the signed path (a reverse proxy mount))
SETUPS = {
    "minio-default-signing": ("http://minio:9000", "http://127.0.0.1:29001", None, "127.0.0.1:29001", ""),
    "minio-sigv4": ("http://minio:9000", "http://127.0.0.1:29001", "s3v4", "127.0.0.1:29001", ""),
    "proxy-sigv4": ("http://minio:9000", "https://hub.example.com", "s3v4", "hub.example.com", ""),
    "proxy-mounted-under-a-path-sigv4": ("http://minio:9000", "https://hub.example.com/s3", "s3v4", "hub.example.com", "/s3"),
    "same-address-sigv4": ("https://s3.example.com", "https://s3.example.com", "s3v4", "s3.example.com", ""),
    "bucket-in-endpoint-same-address": (
        "https://acct.r2.cloudflarestorage.com/real",
        "https://acct.r2.cloudflarestorage.com/real",
        "s3v4",
        "acct.r2.cloudflarestorage.com",
        "",
    ),
    "bucket-in-endpoint-other-host": (
        "https://acct.r2.cloudflarestorage.com/real",
        "https://files.example.com/real",
        "s3v4",
        "files.example.com",
        "",
    ),
}


class InProcessPool:
    """The process pool's ``map``, run in this process (the live test uses a real pool)."""

    def map(self, func, items):
        return map(func, items)


@pytest.fixture
def configure(monkeypatch):
    def apply(endpoint, public, signature_version):
        monkeypatch.setattr(s3_module.cfg.s3, "endpoint", endpoint)
        monkeypatch.setattr(s3_module.cfg.s3, "public_endpoint", public)
        monkeypatch.setattr(s3_module.cfg.s3, "access_key", ACCESS_KEY)
        monkeypatch.setattr(s3_module.cfg.s3, "secret_key", SECRET_KEY)
        monkeypatch.setattr(s3_module.cfg.s3, "region", "us-east-1")
        monkeypatch.setattr(s3_module.cfg.s3, "bucket", BUCKET)
        monkeypatch.setattr(s3_module.cfg.s3, "signature_version", signature_version)
        monkeypatch.setattr(s3_module.cfg.s3, "force_path_style", True)
        monkeypatch.setattr(s3_module, "get_presigned_url_process_pool", lambda: InProcessPool())

    return apply


def sigv4_matches(url: str, method: str, host: str, path_prefix: str = "") -> bool:
    """Recompute the SigV4 query signature of ``url`` for a request with ``Host: host``
    (AWS "Authenticating requests: using query parameters"); a mount prefix the
    proxy strips is not part of what the store sees."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    given = dict(query)["X-Amz-Signature"]
    encode = lambda text: quote(text, safe="-_.~")
    canonical_query = "&".join(f"{encode(k)}={encode(v)}" for k, v in sorted((k, v) for k, v in query if k != "X-Amz-Signature"))
    path = parts.path.removeprefix(path_prefix)
    canonical_request = "\n".join([method, path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    access_key, date, region, service, terminator = dict(query)["X-Amz-Credential"].split("/")
    scope = "/".join([date, region, service, terminator])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", dict(query)["X-Amz-Date"], scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    key = ("AWS4" + SECRET_KEY).encode()
    for step in (date, region, service, terminator):
        key = hmac.new(key, step.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest() == given


def every_url(parts: int = 3) -> dict[str, tuple[str, str]]:
    """The URL of each way a URL is built: ``name -> (url, HTTP method)``."""
    urls = {
        "download": (s3_module._generate_download_presigned_url_sync(BUCKET, OBJECT_KEY), "GET"),
        "upload": (s3_module._generate_upload_presigned_url_sync(BUCKET, OBJECT_KEY)["url"], "PUT"),
    }
    for count in (1, 3, 10, 11, 25):  # up to 10 parts is built one by one, more in the process pool
        part = s3_module._generate_multipart_upload_urls_sync(BUCKET, OBJECT_KEY, count, upload_id="upload-1")["part_urls"][-1]["url"]
        urls[f"part of {count}"] = (part, "PUT")
    return urls


@pytest.mark.parametrize("setup", SETUPS)
def test_every_url_goes_to_the_public_address_and_is_signed_for_it(configure, setup):
    endpoint, public, signature_version, host, prefix = SETUPS[setup]
    configure(endpoint, public, signature_version)

    for name, (url, method) in every_url().items():
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{prefix}" == public.rstrip("/").removesuffix("/real"), name
        if signature_version == "s3v4":
            assert sigv4_matches(url, method, host, prefix), f"{name}: the signature does not cover Host {host}"
            internal = urlsplit(endpoint).netloc
            if internal != host:
                assert not sigv4_matches(url, method, internal, prefix), f"{name}: the signature also fits the internal host"


@pytest.mark.parametrize("setup", SETUPS)
def test_the_signing_algorithm_does_not_depend_on_how_the_url_is_built(configure, setup):
    endpoint, public, signature_version, host, prefix = SETUPS[setup]
    configure(endpoint, public, signature_version)

    urls = every_url()
    algorithms = {name: "SigV4" if "X-Amz-Algorithm" in url else "SigV2" for name, (url, _) in urls.items()}
    assert len(set(algorithms.values())) == 1, algorithms
    names = {name: sorted(k for k, _ in parse_qsl(urlsplit(url).query) if k not in ("X-Amz-Signature", "X-Amz-Date", "Signature", "Expires")) for name, (url, _) in urls.items()}
    assert names["part of 3"] == names["part of 11"] == names["part of 25"], names


def test_a_bucket_in_the_endpoint_keeps_its_path_and_its_key_prefix(configure):
    """Production: the endpoint and the public endpoint are one R2 address with the bucket in its path."""
    endpoint = "https://acct.r2.cloudflarestorage.com/real"
    configure(endpoint, endpoint, "s3v4")

    for name, (url, method) in every_url().items():
        assert urlsplit(url).path == f"/real/{BUCKET}/{OBJECT_KEY}", name
        assert sigv4_matches(url, method, "acct.r2.cloudflarestorage.com"), name


@pytest.mark.parametrize(
    ("endpoint", "public", "expected"),
    [
        ("http://minio:9000", "http://127.0.0.1:29001", "http://127.0.0.1:29001"),
        ("http://minio:9000", "http://minio:9000", "http://minio:9000"),
        ("http://minio:9000", "", "http://minio:9000"),
        ("http://minio:9000", None, "http://minio:9000"),
        ("http://minio:9000/", "http://127.0.0.1:29001", "http://127.0.0.1:29001"),
        ("http://minio:9000", "https://hub.example.com/s3", "https://hub.example.com"),
        ("https://acct.r2.cloudflarestorage.com/real", "https://files.example.com/real", "https://files.example.com/real"),
        ("https://acct.r2.cloudflarestorage.com/real", "https://acct.r2.cloudflarestorage.com/real", "https://acct.r2.cloudflarestorage.com/real"),
    ],
)
def test_the_signing_endpoint_is_the_public_origin_with_the_endpoints_path(endpoint, public, expected):
    assert s3_module._signing_endpoint(endpoint, public) == expected


def test_the_internal_client_keeps_signing_for_the_internal_address(configure):
    """What the API itself calls (copy, list, delete, multipart completion) is unchanged."""
    configure("http://minio:9000", "http://127.0.0.1:29001", "s3v4")

    assert s3_module.get_s3_client().meta.endpoint_url == "http://minio:9000"


def test_a_trailing_slash_on_the_public_endpoint_does_not_double_the_slash(configure):
    configure("http://minio:9000", "http://127.0.0.1:29001/", None)

    url, _ = every_url()["download"]

    assert urlsplit(url).path == f"/{BUCKET}/{OBJECT_KEY}"
