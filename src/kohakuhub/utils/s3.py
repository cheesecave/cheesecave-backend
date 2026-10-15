"""S3 client utilities and helper functions."""

import hashlib
import atexit
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse, urlunparse

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.config import cfg
from kohakuhub.logger import get_logger

logger = get_logger("S3")

# Global process pool for CPU-intensive presigned URL generation
_presigned_url_process_pool = None


def get_presigned_url_process_pool():
    """Get or create global process pool for presigned URL generation."""
    global _presigned_url_process_pool
    if _presigned_url_process_pool is None:
        # Use process pool for CPU-bound signature calculation
        _presigned_url_process_pool = ProcessPoolExecutor(max_workers=8)
        atexit.register(_presigned_url_process_pool.shutdown)
    return _presigned_url_process_pool


def _generate_single_part_url(args: tuple) -> dict:
    """
    Generate presigned URL for a single part (picklable for multiprocessing).

    Args:
        args: Tuple of (bucket, key, upload_id, part_number, expires_in, s3_config),
            ``s3_config`` being ``_presign_settings()``

    Returns:
        Dict with part_number and url
    """
    bucket, key, upload_id, part_number, expires_in, s3_config = args

    # The client is built in each process, the way every other presigned URL is
    url = _presigned_url(
        _presign_client(s3_config),
        s3_config,
        "upload_part",
        {"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number},
        expires_in,
    )
    return {"part_number": part_number, "url": url}


def get_multipart_threshold() -> int:
    """Get multipart upload threshold from config.

    Returns:
        Threshold in bytes for when to use multipart upload
    """
    return cfg.app.lfs_multipart_threshold_bytes


def get_multipart_chunk_size() -> int:
    """Get multipart upload chunk size from config.

    Returns:
        Chunk size in bytes for each part in multipart upload
    """
    return cfg.app.lfs_multipart_chunk_size_bytes


# Backward compatibility: Export as constants (values read from config)
# These will always return current config values
MULTIPART_THRESHOLD = property(lambda self: get_multipart_threshold())
MULTIPART_CHUNK_SIZE = property(lambda self: get_multipart_chunk_size())


def bucket_in_endpoint(
    endpoint: str | None = None, bucket: str | None = None
) -> tuple[str, str, str] | None:
    """``(root endpoint, real bucket, key prefix)`` when the endpoint carries the
    real bucket in its path, as ``https://<account>.r2.cloudflarestorage.com/
    <bucket>``: ``bucket`` is then a key prefix inside that bucket, after any
    further path. ``None`` for a plain endpoint (#133). Both default to the
    configured ones."""
    parsed = urlparse((cfg.s3.endpoint if endpoint is None else endpoint) or "")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    prefix = "/".join(parts[1:] + [cfg.s3.bucket if bucket is None else bucket]) + "/"
    return f"{parsed.scheme}://{parsed.netloc}", parts[0], prefix


class _BucketInEndpoint:
    """Presents ``cfg.s3.bucket`` as a bucket on a client of the root endpoint.

    S3 reads the first path segment as the bucket, and an SDK only prepends an
    endpoint's path to the URL: requests naming a key went where they should,
    but a copy's source (a header) and bucket-level requests (listing, bulk
    delete, bucket checks) did not (#133). Requests naming the configured
    bucket now name the real one, keys under the prefix, and listings never
    leave it; the keys S3 answers come back without the prefix.
    """

    KEYS = ("Key", "StartAfter", "Marker", "KeyMarker")  # what requests name under the prefix
    ANSWERED = ("Key", "Prefix", "StartAfter", "Marker", "NextMarker", "KeyMarker", "NextKeyMarker")
    LISTED = ("Contents", "Versions", "DeleteMarkers", "Deleted", "Errors", "Uploads", "CommonPrefixes")

    def __init__(self, bucket: str, real: str, prefix: str):
        self.bucket, self.real, self.prefix = bucket, real, prefix

    def request(self, params, model, context, **kwargs):
        """``before-parameter-build``."""
        if "CopySource" in params:
            params["CopySource"] = self._source(params["CopySource"])
        if params.get("Bucket") != self.bucket:
            return
        params["Bucket"] = self.real
        context["bucket_in_endpoint"] = True
        for name in self.KEYS:
            if name in params:
                params[name] = self.prefix + params[name]
        if "Prefix" in model.input_shape.members:  # a listing never leaves the prefix
            params["Prefix"] = self.prefix + params.get("Prefix", "")
        if "Delete" in params:  # a new list: the caller's is left as it was
            objects = [{**item, "Key": self.prefix + item["Key"]} for item in params["Delete"]["Objects"]]
            params["Delete"] = {**params["Delete"], "Objects": objects}

    def _source(self, source: str) -> str:
        """A copy source arrives as the header's string: botocore's own handler
        for the operation runs first and URL-quotes a dict's key."""
        bucket, _, rest = source.lstrip("/").partition("/")
        if bucket != self.bucket:
            return source
        return f"{self.real}/{quote(self.prefix, safe='/~')}{rest}"

    def response(self, parsed, context, **kwargs):
        """``after-call``: runs last, after botocore decoded listed keys."""
        if not context.get("bucket_in_endpoint"):
            return
        self._relative(parsed)
        for name in self.LISTED:
            for item in parsed.get(name) or []:
                self._relative(item)

    def _relative(self, item: dict) -> None:
        for name in self.ANSWERED:
            value = item.get(name)
            if isinstance(value, str) and value.startswith(self.prefix):
                item[name] = value[len(self.prefix) :]
        for name in ("Bucket", "Name"):
            if item.get(name) == self.real:
                item[name] = self.bucket


def readdress(client, bucket: str, layout: tuple[str, str, str]):
    """Hook ``client``, made for ``layout``'s root endpoint, so that ``bucket``
    reads as a bucket (``_BucketInEndpoint``); returns the client."""
    hooks = _BucketInEndpoint(bucket, layout[1], layout[2])
    client.meta.events.register_first("before-parameter-build.s3", hooks.request)
    client.meta.events.register_last("after-call.s3", hooks.response)
    return client


def _client_settings() -> dict:
    """What a client needs, from the configuration (picklable)."""
    return {
        "access_key": cfg.s3.access_key,
        "secret_key": cfg.s3.secret_key,
        "region": cfg.s3.region,
        "signature_version": cfg.s3.signature_version,
        "force_path_style": cfg.s3.force_path_style,
        "bucket": cfg.s3.bucket,
    }


def _build_client(endpoint: str, settings: dict, pool: int | None = None):
    """A client of ``endpoint`` (a path in it names the real bucket, see
    ``bucket_in_endpoint``) for ``settings`` (``_client_settings()``); ``pool``
    connections for callers that run many requests at once (boto3 keeps 10)."""
    # Build S3-specific config
    s3_config = {}
    extra = {"max_pool_connections": pool} if pool else {}

    if settings["force_path_style"]:
        s3_config["addressing_style"] = "path"

    # Use configured signature version
    sig_version = settings["signature_version"]
    if sig_version:
        logger.debug(f"Using S3 signature version: {sig_version}")
        boto_config = BotoConfig(
            signature_version=sig_version,
            s3=s3_config,
            **extra,
        )
    else:
        logger.debug("Using default S3 signature version (s3v2 compatible)")
        boto_config = BotoConfig(
            s3=s3_config,
            **extra,
        )

    layout = bucket_in_endpoint(endpoint, settings["bucket"])
    client = boto3.client(
        "s3",
        endpoint_url=layout[0] if layout else endpoint,
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
        config=boto_config,
    )
    return readdress(client, settings["bucket"], layout) if layout else client


def get_s3_client(pool: int | None = None):
    """Create configured S3 client with configurable signature version.

    Signature versions:
    - None: Use default (s3v2 for MinIO compatibility)
    - "s3v4": AWS S3, Cloudflare R2 (required for these services)

    Set via KOHAKU_HUB_S3_SIGNATURE_VERSION environment variable. An endpoint
    with a path names the real bucket (``bucket_in_endpoint``): the client then
    talks to the root endpoint and keeps ``cfg.s3.bucket`` as a key prefix.

    This client is the API's own, for the endpoint it reaches the store at;
    presigned URLs for clients are made with ``_presign_client``.

    Returns:
        Configured boto3 S3 client.
    """
    return _build_client(cfg.s3.endpoint, _client_settings(), pool)


def _signing_endpoint(endpoint: str, public_endpoint: str | None) -> str:
    """The endpoint a presigned URL is signed for: the public address, in the
    shape of the internal endpoint (its path names the real bucket).

    The store checks a signature against the ``Host`` the request carries, and
    SigV4 signs it, so a URL signed for the internal address and then pointed at
    the public one is refused (``SignatureDoesNotMatch``). A path on the public
    address that the internal one does not have is a proxy's mount point: it is
    added after signing (``_presigned_url``), as the proxy removes it again.
    """
    if not public_endpoint or public_endpoint.rstrip("/") == endpoint.rstrip("/"):
        return endpoint
    internal, public = urlparse(endpoint), urlparse(public_endpoint)
    return urlunparse((public.scheme, public.netloc, internal.path.rstrip("/"), "", "", ""))


def _public_base(endpoint: str, public_endpoint: str | None) -> str:
    """What the signing endpoint is replaced with in a URL: the public endpoint.

    With the real bucket in the endpoint's path the URL has to carry it. A public
    address that ends with it already has it; one that does not (a proxy mounted
    on ``/s3``) is the mount, and the bucket follows it.
    """
    if not public_endpoint:
        return endpoint.rstrip("/")
    bucket_path = urlparse(endpoint).path.rstrip("/")
    public = public_endpoint.rstrip("/")
    if bucket_path and not urlparse(public).path.endswith(bucket_path):
        public += bucket_path
    return public


def _presign_settings() -> dict:
    """What presigning needs from the configuration (picklable, for the process pool)."""
    return {
        **_client_settings(),
        "endpoint": cfg.s3.endpoint,
        "public_endpoint": cfg.s3.public_endpoint,
    }


def _presign_client(settings: dict):
    """The client every presigned URL is made with, whatever its kind or part
    count: it signs for the public address."""
    return _build_client(_signing_endpoint(settings["endpoint"], settings["public_endpoint"]), settings)


def _presigned_url(client, settings: dict, operation: str, params: dict, expires_in: int, **extra) -> str:
    """``operation``'s URL for a client of the public endpoint."""
    url = client.generate_presigned_url(operation, Params=params, ExpiresIn=expires_in, **extra)
    signed_for = _signing_endpoint(settings["endpoint"], settings["public_endpoint"])
    return url.replace(signed_for, _public_base(settings["endpoint"], settings["public_endpoint"]))


def init_storage():
    """Check and create the configured S3 bucket if it doesn't exist."""
    s3 = get_s3_client()
    bucket_name = cfg.s3.bucket
    region = cfg.s3.region

    try:
        s3.head_bucket(Bucket=bucket_name)
        logger.success(f"S3 Bucket '{bucket_name}' already exists.")
    except Exception as e:
        # If a 404 error is received, the bucket does not exist
        error_code = e.response["Error"]["Code"]
        if error_code == "404":
            logger.info(f"S3 Bucket '{bucket_name}' not found. Creating it...")
            try:
                # MinIO doesn't care about the region for local setup, but S3 requires it
                if region and region != "us-east-1":
                    s3.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={"LocationConstraint": region},
                    )
                else:
                    s3.create_bucket(Bucket=bucket_name)

                logger.success(f"S3 Bucket '{bucket_name}' created successfully.")
            except Exception as e:
                logger.exception(f"Failed to create S3 bucket '{bucket_name}'", e)
                # You might want to raise this error to halt startup
                raise
        else:
            # Other error (e.g., 403 Forbidden)
            logger.exception(f"Error checking S3 bucket '{bucket_name}'", e)
            raise


def _generate_download_presigned_url_sync(
    bucket: str, key: str, expires_in: int = 3600, filename: str = None
) -> str:
    """Synchronous implementation of generate_download_presigned_url."""
    settings = _presign_settings()
    params = {"Bucket": bucket, "Key": key}

    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}";'

    return _presigned_url(_presign_client(settings), settings, "get_object", params, expires_in)


async def generate_download_presigned_url(
    bucket: str, key: str, expires_in: int = 3600, filename: str = None
) -> str:
    """Generate presigned URL for downloading from S3.

    Args:
        bucket: S3 bucket name
        key: Object key in S3
        expires_in: URL expiration time in seconds (default: 1 hour)
        filename: Optional filename for Content-Disposition header

    Returns:
        Presigned download URL
    """
    return await run_in_s3_executor(
        _generate_download_presigned_url_sync, bucket, key, expires_in, filename
    )


def _generate_upload_presigned_url_sync(
    bucket: str,
    key: str,
    expires_in: int = 3600,
    content_type: str = None,
    checksum_sha256: str = None,
) -> dict:
    """Synchronous implementation of generate_upload_presigned_url."""
    settings = _presign_settings()

    params = {
        "Bucket": bucket,
        "Key": key,
    }

    # Only include ContentType in params if provided
    # This makes it part of the signature - client MUST send matching Content-Type
    if content_type:
        params["ContentType"] = content_type

    url = _presigned_url(
        _presign_client(settings), settings, "put_object", params, expires_in, HttpMethod="PUT"
    )

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    headers = {}

    # Include Content-Type in response headers if it was in params
    if content_type:
        headers["Content-Type"] = content_type

    return {
        "url": url,
        "expires_at": expires_at,
        "method": "PUT",
        "headers": headers,
    }


async def generate_upload_presigned_url(
    bucket: str,
    key: str,
    expires_in: int = 3600,
    content_type: str = None,
    checksum_sha256: str = None,
) -> dict:
    """Generate presigned URL for uploading to S3.

    Args:
        bucket: S3 bucket name
        key: Object key in S3
        expires_in: URL expiration time in seconds (default: 1 hour)
        content_type: Content type of the object
        checksum_sha256: Base64-encoded SHA256 checksum for S3 to verify (optional)

    Returns:
        Dict with 'url', 'fields', and 'expires_at'
    """
    return await run_in_s3_executor(
        _generate_upload_presigned_url_sync,
        bucket,
        key,
        expires_in,
        content_type,
        checksum_sha256,
    )


def _generate_multipart_upload_urls_sync(
    bucket: str,
    key: str,
    part_count: int,
    upload_id: str = None,
    expires_in: int = 3600,
) -> dict:
    """Synchronous implementation of generate_multipart_upload_urls."""
    if not upload_id:
        # The API opens the upload at the internal endpoint
        response = get_s3_client().create_multipart_upload(
            Bucket=bucket,
            Key=key,
            ContentType="application/octet-stream",
        )
        upload_id = response["UploadId"]

    # Prepare S3 config for multiprocessing (must be picklable)
    s3_config = _presign_settings()

    # For large part counts (>10), use multiprocessing for parallel signature generation
    if part_count > 10:
        logger.info(
            f"Generating {part_count} presigned URLs in parallel (multiprocessing)..."
        )

        # Prepare arguments for each part
        args_list = [
            (bucket, key, upload_id, part_number, expires_in, s3_config)
            for part_number in range(1, part_count + 1)
        ]

        # Generate URLs in parallel using process pool
        pool = get_presigned_url_process_pool()
        part_urls = list(pool.map(_generate_single_part_url, args_list))

        logger.success(f"Generated {part_count} presigned URLs in parallel")
    else:
        # For small part counts, use sequential generation (faster due to no overhead)
        client = _presign_client(s3_config)
        part_urls = [
            {
                "part_number": part_number,
                "url": _presigned_url(
                    client,
                    s3_config,
                    "upload_part",
                    {
                        "Bucket": bucket,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    expires_in,
                ),
            }
            for part_number in range(1, part_count + 1)
        ]

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    return {
        "upload_id": upload_id,
        "part_urls": part_urls,
        "expires_at": expires_at,
    }


async def generate_multipart_upload_urls(
    bucket: str,
    key: str,
    part_count: int,
    upload_id: str = None,
    expires_in: int = 3600,
) -> dict:
    """Generate presigned URLs for multipart upload.

    For files larger than 5GB, S3 requires multipart upload.

    Args:
        bucket: S3 bucket name
        key: Object key in S3
        part_count: Number of parts to upload
        upload_id: Existing upload ID (if resuming)
        expires_in: URL expiration time in seconds

    Returns:
        Dict with 'upload_id', 'part_urls', and 'expires_at'
    """
    return await run_in_s3_executor(
        _generate_multipart_upload_urls_sync,
        bucket,
        key,
        part_count,
        upload_id,
        expires_in,
    )


def _complete_multipart_upload_sync(
    bucket: str, key: str, upload_id: str, parts: list
) -> dict:
    """Synchronous implementation of complete_multipart_upload."""
    s3 = get_s3_client()

    response = s3.complete_multipart_upload(
        Bucket=bucket,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )

    return response


async def complete_multipart_upload(
    bucket: str, key: str, upload_id: str, parts: list
) -> dict:
    """Complete a multipart upload.

    Args:
        bucket: S3 bucket name
        key: Object key in S3
        upload_id: Upload ID from create_multipart_upload
        parts: List of dicts with 'PartNumber' and 'ETag'

    Returns:
        S3 completion response
    """
    return await run_in_s3_executor(
        _complete_multipart_upload_sync, bucket, key, upload_id, parts
    )


def _abort_multipart_upload_sync(bucket: str, key: str, upload_id: str):
    """Synchronous implementation of abort_multipart_upload."""
    s3 = get_s3_client()
    s3.abort_multipart_upload(
        Bucket=bucket,
        Key=key,
        UploadId=upload_id,
    )


async def abort_multipart_upload(bucket: str, key: str, upload_id: str):
    """Abort a multipart upload.

    Args:
        bucket: S3 bucket name
        key: Object key in S3
        upload_id: Upload ID to abort
    """
    await run_in_s3_executor(_abort_multipart_upload_sync, bucket, key, upload_id)


def _get_object_metadata_sync(bucket: str, key: str) -> dict:
    """Synchronous implementation of get_object_metadata."""
    s3 = get_s3_client()
    response = s3.head_object(Bucket=bucket, Key=key)

    return {
        "size": response.get("ContentLength"),
        "etag": response.get("ETag", "").strip('"'),
        "last_modified": response.get("LastModified"),
        "content_type": response.get("ContentType"),
    }


async def get_object_metadata(bucket: str, key: str) -> dict:
    """Get metadata for an S3 object.

    Args:
        bucket: S3 bucket name
        key: Object key in S3

    Returns:
        Dict with 'size', 'etag', 'last_modified', 'content_type'

    Raises:
        ClientError: If object not found
    """
    return await run_in_s3_executor(_get_object_metadata_sync, bucket, key)


def _object_sha256_sync(bucket: str, key: str) -> str:
    """Synchronous implementation of object_sha256: hash while streaming."""
    digest = hashlib.sha256()
    body = get_s3_client().get_object(Bucket=bucket, Key=key)["Body"]
    try:
        for chunk in body.iter_chunks(8 * 1024 * 1024):
            digest.update(chunk)
    finally:
        body.close()
    return digest.hexdigest()


async def object_sha256(bucket: str, key: str) -> str:
    """SHA-256 of a stored object, read in chunks (never held in memory).

    Raises:
        ClientError: If object not found
    """
    return await run_in_s3_executor(_object_sha256_sync, bucket, key)


NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound")


def _object_exists_sync(bucket: str, key: str) -> bool:
    """Synchronous implementation of object_exists."""
    s3 = get_s3_client()

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in NOT_FOUND_CODES:
            return False
        raise  # a permission error or a store failing is not a missing object


async def object_exists(bucket: str, key: str) -> bool:
    """Check if an object exists in S3.

    Args:
        bucket: S3 bucket name
        key: Object key in S3

    Returns:
        True if object exists, False if the store says it does not (#10)

    Raises:
        ClientError / BotoCoreError: the store could not answer (a permission error, an
            outage): that is not "missing", and what asks must not take it for that.
    """
    return await run_in_s3_executor(_object_exists_sync, bucket, key)


def parse_s3_uri(uri: str) -> tuple:
    """Parse S3 URI into bucket and key.

    Args:
        uri: S3 URI (e.g., "s3://bucket/path/to/file")

    Returns:
        Tuple of (bucket, key)

    Raises:
        ValueError: If URI is invalid
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {uri}")

    parts = uri[5:].split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""

    return bucket, key


class S3DeleteFailed(RuntimeError):
    """The store refused to delete some of the objects it was asked to."""


def _delete_objects_with_prefix_sync(bucket: str, prefix: str) -> int:
    """Synchronous implementation of delete_objects_with_prefix.

    Deletes all objects under the prefix, in batches of 1000 (the S3 limit).

    Args:
        bucket: S3 bucket name
        prefix: Prefix to delete (e.g., "hf-model-user-repo/")

    Returns:
        Number of objects deleted

    Raises:
        ClientError / BotoCoreError: listing or deleting failed
        S3DeleteFailed: the store refused some objects (what was deleted stays deleted;
            running it again deletes the rest)
    """
    s3_client = get_s3_client()
    deleted_count = 0

    paginator = s3_client.get_paginator("list_objects_v2")
    objects_to_delete = [
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]
    if not objects_to_delete:
        logger.info(f"No objects found with prefix: {prefix}")
        return 0

    logger.info(f"Found {len(objects_to_delete)} object(s) to delete with prefix: {prefix}")
    batch_size = 1000
    for i in range(0, len(objects_to_delete), batch_size):
        batch = objects_to_delete[i : i + batch_size]
        response = s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": False},
        )
        deleted_count += len(response.get("Deleted", []))
        if errors := response.get("Errors"):
            first = errors[0]
            raise S3DeleteFailed(
                f"S3 refused to delete {len(errors)} object(s) under {prefix}, e.g. {first.get('Key')}: "
                f"{first.get('Code')} {first.get('Message')}"
            )

    logger.success(f"Deleted {deleted_count} object(s) from S3 with prefix: {prefix}")
    return deleted_count


async def delete_objects_with_prefix(bucket: str, prefix: str) -> int:
    """Delete all objects under a given prefix.

    Args:
        bucket: S3 bucket name
        prefix: Prefix to delete (e.g., "hf-model-user-repo/")

    Returns:
        Number of objects deleted

    Raises:
        ClientError / BotoCoreError / S3DeleteFailed: see ``_delete_objects_with_prefix_sync``
    """
    return await run_in_s3_executor(_delete_objects_with_prefix_sync, bucket, prefix)
