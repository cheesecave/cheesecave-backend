"""Git LFS Batch API implementation - Refactored version.

This module implements the Git LFS Batch API specification for handling
large file uploads (>10MB). It provides presigned S3 URLs for direct uploads.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kohakuhub.config import cfg
from kohakuhub.db import Repository, User
from kohakuhub.db_operations import get_file_by_sha256, get_organization, get_repository
from kohakuhub.lfs_gc import tombstone_state, touch
from kohakuhub.logger import get_logger
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import (
    RepoReadDeniedError,
    check_repo_read_permission,
    check_repo_write_permission,
)
from kohakuhub.utils.s3 import (
    complete_multipart_upload,
    generate_download_presigned_url,
    generate_multipart_upload_urls,
    generate_upload_presigned_url,
    get_multipart_chunk_size,
    get_multipart_threshold,
    get_object_metadata,
    object_exists,
)
from kohakuhub.api.quota.util import check_quota

logger = get_logger("LFS")
router = APIRouter()


class LFSObject(BaseModel):
    """LFS object specification."""

    oid: str  # SHA256 hash
    size: int


class LFSBatchRequest(BaseModel):
    """LFS batch API request."""

    operation: str  # "upload" or "download"
    transfers: list[str] | None = ["basic"]
    objects: list[LFSObject]
    hash_algo: str | None = "sha256"
    is_browser: bool = (
        False  # True if request from browser (needs Content-Type in signature)
    )


class LFSError(BaseModel):
    """LFS error object - must have 'code' (int) and 'message' (str)."""

    code: int  # HTTP status code as integer
    message: str  # Error description


class LFSObjectResponse(BaseModel):
    """LFS object response with actions."""

    oid: str
    size: int
    authenticated: bool | None = True
    actions: dict | None = None  # Contains "upload", "verify", "download"
    error: LFSError | None = None  # Must use LFSError model


class LFSBatchResponse(BaseModel):
    """LFS batch API response."""

    transfer: str = "basic"
    objects: list[LFSObjectResponse]
    hash_algo: str = "sha256"


def get_lfs_key(oid: str) -> str:
    """Generate S3 key for LFS object.

    Args:
        oid: SHA256 hash

    Returns:
        S3 key with balanced directory structure
    """
    return f"lfs/{oid[:2]}/{oid[2:4]}/{oid}"


# ---------------------------------------------------------------------------
# Authorization helpers
#
# LFS objects live under one global key space (``get_lfs_key``), so nothing in
# the URL of the follow-up routes scopes an upload to a repository. The batch
# route is where authorization and quota are decided; it hands out URLs for
# the two follow-up routes (``complete`` and ``verify``) that carry a signed
# ticket bound to the repository, the object (and the multipart upload). The
# follow-up routes accept that ticket, because real clients send no credentials
# on those calls (``huggingface_hub`` on ``complete``, the web UI on both), or
# a signed-in caller who can write to the repository named in the URL.
# ---------------------------------------------------------------------------

LFS_TICKET_TTL_SECONDS = 86400 * 7  # as long as the multipart part URLs live
_OID_PATTERN = re.compile(r"[0-9a-f]{64}")
_REPO_TYPES = ("model", "dataset", "space")
_LFS_CHALLENGE = {
    "LFS-Authenticate": 'Basic realm="Authentication required", charset="UTF-8"'
}


def is_valid_oid(oid) -> bool:
    """True for a lowercase SHA-256 hex digest, the only object id LFS uses."""
    return isinstance(oid, str) and _OID_PATTERN.fullmatch(oid) is not None


def _ticket_signature(purpose, repo_id, oid, upload_id, expires) -> str:
    key = hmac.new(
        cfg.auth.session_secret.encode(), b"kohakuhub-lfs-ticket-v1", hashlib.sha256
    ).digest()
    # JSON keeps the fields apart: no value can be shifted into its neighbour.
    message = json.dumps(
        [purpose, repo_id, oid, upload_id, expires], separators=(",", ":")
    )
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def issue_lfs_ticket(
    purpose: str,
    repo_id: str,
    oid: str,
    upload_id: str | None = None,
    ttl: int = LFS_TICKET_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Sign a short-lived permission for one follow-up call on one object."""
    expires = int((time.time() if now is None else now) + ttl)
    return f"{expires}.{_ticket_signature(purpose, repo_id, oid, upload_id, expires)}"


def check_lfs_ticket(
    ticket,
    purpose: str,
    repo_id: str,
    oid: str,
    upload_id: str | None = None,
    now: float | None = None,
) -> bool:
    """True when ``ticket`` was issued for exactly this call and has not expired."""
    if not isinstance(ticket, str):
        return False
    expires_text, dot, signature = ticket.partition(".")
    if not dot or not expires_text.isdigit():
        return False
    expires = int(expires_text)
    if expires < (time.time() if now is None else now):
        return False
    expected = _ticket_signature(purpose, repo_id, oid, upload_id, expires)
    # bytes: compare_digest refuses non-ASCII str, and this value comes from a URL
    return hmac.compare_digest(signature.encode(), expected.encode())


def _with_ticket(url: str, ticket: str) -> str:
    return f"{url}?ticket={ticket}"


def _unauthorized() -> HTTPException:
    return HTTPException(
        401, detail={"error": "Authentication required"}, headers=dict(_LFS_CHALLENGE)
    )


def _repo_not_found() -> HTTPException:
    return HTTPException(
        404,
        detail={"error": "Repository not found"},
        headers={"X-Error-Code": "RepoNotFound"},
    )


def _not_visible(user: User | None) -> HTTPException:
    """A repository that is missing, or that the caller may not see.

    Both get the same answer, so the response never tells them apart: an
    anonymous caller is asked to authenticate, a signed-in one gets a 404.
    """
    return _unauthorized() if user is None else _repo_not_found()


def _can_read(repo: Repository, user: User | None) -> bool:
    try:
        check_repo_read_permission(repo, user)
    except RepoReadDeniedError:
        return False
    return True


def _authorize_batch(repo: Repository | None, operation: str, user: User | None):
    """Decide a batch request before anything is generated for it."""
    if repo is None or not _can_read(repo, user):
        raise _not_visible(user)
    if operation == "upload":
        if user is None:
            raise _unauthorized()
        check_repo_write_permission(repo, user)


def _authorize_followup(
    namespace: str,
    name: str,
    user: User | None,
    ticket: str | None,
    purpose: str,
    oid: str,
    upload_id: str | None = None,
):
    """Authorize ``complete`` / ``verify``: a valid ticket, or a repository writer.

    The URL names a repository but not its type, so the caller needs write
    permission on at least one of the repositories with that name.
    """
    if ticket and check_lfs_ticket(
        ticket, purpose, f"{namespace}/{name}", oid, upload_id
    ):
        return
    if user is None:
        raise _unauthorized()

    readable = False
    for repo_type in _REPO_TYPES:
        repo = get_repository(repo_type, namespace, name)
        if repo is None or not _can_read(repo, user):
            continue
        readable = True
        try:
            check_repo_write_permission(repo, user)
        except HTTPException:
            continue
        return
    if readable:
        raise HTTPException(
            403,
            detail={"error": "You don't have permission to modify this repository"},
        )
    raise _repo_not_found()


async def process_upload_object(
    oid: str, size: int, repo_id: str, is_browser: bool = False
) -> LFSObjectResponse:
    """Process single LFS object for upload operation.

    Args:
        oid: Object ID (SHA256)
        size: File size in bytes
        repo_id: Repository ID

    Returns:
        LFS object response with upload actions or error
    """
    lfs_key = get_lfs_key(oid)

    # Check if object exists in S3 (global dedup)
    try:
        s3_exists = await object_exists(cfg.s3.bucket, lfs_key)
    except Exception as e:
        logger.exception(
            f"Failed to check S3 existence for {oid[:8]} (key: {lfs_key})", e
        )
        s3_exists = False

    # Only storage says whether the content exists: a File row can outlive a
    # collected object, and an object being collected must be uploaded again.
    # Either way the upload-then-commit round trip is protected from a
    # collection by the recent-object grace period (#114).
    touch(oid)  # first, so a collection deciding after this keeps it
    collected = tombstone_state(oid) is not None

    if s3_exists and not collected:
        # Tell client to skip upload
        logger.info(f"LFS object {oid[:8]} already exists, skipping upload")
        return LFSObjectResponse(
            oid=oid,
            size=size,
            authenticated=True,
            # No actions = already exists
        )

    # Check if multipart upload required
    # HuggingFace clients detect multipart by presence of 'chunk_size' in header
    multipart_threshold = get_multipart_threshold()
    multipart_chunk_size = get_multipart_chunk_size()
    use_multipart = size > multipart_threshold

    if use_multipart:
        # Multipart upload for large files
        try:
            # Calculate number of parts needed
            part_count = (size + multipart_chunk_size - 1) // multipart_chunk_size

            # S3 has a hard limit of 10,000 parts per upload
            MAX_PARTS = 10000
            if part_count > MAX_PARTS:
                # Increase chunk size to stay within limit
                adjusted_chunk_size = (size + MAX_PARTS - 1) // MAX_PARTS
                # Round up to nearest MB for cleaner chunks
                adjusted_chunk_size = (
                    (adjusted_chunk_size + 1048575) // 1048576
                ) * 1048576
                part_count = (size + adjusted_chunk_size - 1) // adjusted_chunk_size

                logger.warning(
                    f"File size {size:,} bytes would require {part_count} parts with {multipart_chunk_size:,} byte chunks. "
                    f"Increased chunk size to {adjusted_chunk_size:,} bytes to stay under {MAX_PARTS} part limit. "
                    f"New part count: {part_count}"
                )
                multipart_chunk_size = adjusted_chunk_size

            # Generate multipart upload URLs with longer expiration for large files
            multipart_info = await generate_multipart_upload_urls(
                bucket=cfg.s3.bucket,
                key=lfs_key,
                part_count=part_count,
                expires_in=86400 * 7,
            )

            logger.info(
                f"Generated multipart upload for {oid[:8]}: "
                f"{part_count} parts, chunk_size={multipart_chunk_size}"
            )

            # Build response compatible with HuggingFace clients
            # The 'chunk_size' in header tells client to use multipart upload
            # Part URLs must be in header with numeric string keys ("1", "2", "3", etc.)
            header = {
                "chunk_size": str(multipart_chunk_size),  # Signal multipart upload
                "upload_id": multipart_info["upload_id"],
            }

            # Add part URLs with numeric keys (HuggingFace client expects this format)
            for part in multipart_info["part_urls"]:
                header[str(part["part_number"])] = part["url"]

            return LFSObjectResponse(
                oid=oid,
                size=size,
                authenticated=True,
                actions={
                    "upload": {
                        "href": _with_ticket(
                            f"{cfg.app.base_url}/api/{repo_id}.git/info/lfs/complete/{multipart_info['upload_id']}",
                            issue_lfs_ticket(
                                "complete", repo_id, oid, multipart_info["upload_id"]
                            ),
                        ),
                        "expires_at": multipart_info["expires_at"],
                        "header": header,
                    },
                    "verify": {
                        "href": _with_ticket(
                            f"{cfg.app.base_url}/api/{repo_id}.git/info/lfs/verify",
                            issue_lfs_ticket("verify", repo_id, oid),
                        ),
                        "expires_at": multipart_info["expires_at"],
                    },
                },
            )
        except Exception as e:
            logger.exception(
                f"Failed to generate multipart upload URLs for LFS object {oid[:8]} "
                f"(size: {size}, parts: {part_count})",
                e,
            )
            return LFSObjectResponse(
                oid=oid,
                size=size,
                error=LFSError(
                    code=500,
                    message=f"Failed to generate multipart upload URLs: {str(e)}",
                ),
            )

    # Single PUT upload for smaller files
    try:
        # Convert SHA256 hex to base64 for S3 checksum verification
        checksum_sha256 = base64.b64encode(bytes.fromhex(oid)).decode("utf-8")

        # For browser uploads, we need to include Content-Type in the presigned URL
        # because browsers always send Content-Type header
        # For CLI clients, we don't include it because they might not send it
        content_type = "application/octet-stream" if is_browser else None

        upload_info = await generate_upload_presigned_url(
            bucket=cfg.s3.bucket,
            key=lfs_key,
            expires_in=86400,
            content_type=content_type,
            checksum_sha256=checksum_sha256,
        )

        return LFSObjectResponse(
            oid=oid,
            size=size,
            authenticated=True,
            actions={
                "upload": {
                    "href": upload_info["url"],
                    "expires_at": upload_info["expires_at"],
                    "header": upload_info.get("headers", {}),
                },
                "verify": {
                    "href": _with_ticket(
                        f"{cfg.app.base_url}/api/{repo_id}.git/info/lfs/verify",
                        issue_lfs_ticket("verify", repo_id, oid, ttl=86400),
                    ),
                    "expires_at": upload_info["expires_at"],
                },
            },
        )
    except Exception as e:
        logger.exception(
            f"Failed to generate upload URL for LFS object {oid[:8]} "
            f"(size: {size}, key: {lfs_key})",
            e,
        )
        return LFSObjectResponse(
            oid=oid,
            size=size,
            error=LFSError(
                code=500,
                message=f"Failed to generate upload URL: {str(e)}",
            ),
        )


async def process_download_object(oid: str, size: int) -> LFSObjectResponse:
    """Process single LFS object for download operation.

    Args:
        oid: Object ID (SHA256)
        size: File size in bytes

    Returns:
        LFS object response with download actions or error
    """
    lfs_key = get_lfs_key(oid)

    # Check if object exists; a collected object's File rows can outlive it
    existing = get_file_by_sha256(oid)

    if not existing or tombstone_state(oid) is not None:
        return LFSObjectResponse(
            oid=oid,
            size=size,
            error=LFSError(code=404, message="Object not found"),
        )

    # Object exists, provide download URL
    try:
        download_url = await generate_download_presigned_url(
            bucket=cfg.s3.bucket,
            key=lfs_key,
            expires_in=86400,
        )

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=3600)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

        return LFSObjectResponse(
            oid=oid,
            size=size,
            authenticated=True,
            actions={
                "download": {
                    "href": download_url,
                    "expires_at": expires_at,
                },
            },
        )
    except Exception as e:
        return LFSObjectResponse(
            oid=oid,
            size=size,
            error=LFSError(
                code=500,
                message=f"Failed to generate download URL: {str(e)}",
            ),
        )


@router.post("/{repo_type}s/{namespace}/{name}.git/info/lfs/objects/batch")
@router.post("/{namespace}/{name}.git/info/lfs/objects/batch")
async def lfs_batch(
    namespace: str,
    name: str,
    request: Request,
    repo_type: str | None = "model",
    user: User | None = Depends(get_optional_user),
):
    """Git LFS Batch API endpoint.

    Handles LFS batch requests for upload/download operations.
    Returns presigned URLs for S3 direct upload/download.

    Args:
        namespace: Repository namespace
        name: Repository name
        request: FastAPI request with LFS batch payload
        repo_type: Repository type (default: model)
        user: Current authenticated user (optional for downloads)

    Returns:
        LFS batch response with actions

    Raises:
        HTTPException: If operation fails
    """
    repo_id = f"{namespace}/{name}"

    # Parse request
    try:
        body = await request.json()
        batch_req = LFSBatchRequest(**body)
    except Exception as e:
        raise HTTPException(400, detail={"error": f"Invalid LFS batch request: {e}"})

    # Get repository using get_repository() which returns Repository FK object
    repo = get_repository(repo_type, namespace, name)

    # Decide before anything is generated: a repository that does not exist or
    # that the caller may not see is answered like an unauthorized request, and
    # an upload needs write permission.
    operation = batch_req.operation
    _authorize_batch(repo, operation, user)

    if operation == "upload":
        # Check storage quota for uploads
        total_upload_bytes = sum(obj.size for obj in batch_req.objects)

        # Check if namespace is organization (User with is_org=True)
        org = get_organization(namespace)
        is_org = org is not None

        # Check quota (based on repo privacy)
        is_private = repo.private
        allowed, error_msg = check_quota(
            namespace, total_upload_bytes, is_private, is_org
        )
        if not allowed:
            raise HTTPException(
                status_code=413,  # Payload Too Large
                detail={
                    "error": "Storage quota exceeded",
                    "message": error_msg,
                },
            )

    if cfg.app.debug_log_payloads:
        logger.debug("==== LFS Batch Request ====")
        logger.debug(body)

    # Process all objects in parallel
    async def process_object(obj: LFSObject) -> LFSObjectResponse:
        """Process single LFS object based on operation type."""
        if not is_valid_oid(obj.oid):
            return LFSObjectResponse(
                oid=obj.oid,
                size=obj.size,
                error=LFSError(code=422, message="Invalid object id"),
            )
        match batch_req.operation:
            case "upload":
                return await process_upload_object(
                    obj.oid, obj.size, repo_id, batch_req.is_browser
                )
            case "download":
                return await process_download_object(obj.oid, obj.size)
            case _:
                return LFSObjectResponse(
                    oid=obj.oid,
                    size=obj.size,
                    error=LFSError(
                        code=400, message=f"Unknown operation: {batch_req.operation}"
                    ),
                )

    objects_response = await asyncio.gather(
        *[process_object(obj) for obj in batch_req.objects]
    )

    # Return response with exclude_none to omit null fields
    response = LFSBatchResponse(
        transfer="basic",
        objects=objects_response,
        hash_algo="sha256",
    )

    return JSONResponse(
        content=response.model_dump(exclude_none=True),
        media_type="application/vnd.git-lfs+json",
    )


@router.post("/api/{namespace}/{name}.git/info/lfs/complete/{upload_id}")
@router.post("/api/{namespace}/{name}.git/info/lfs/complete")
async def lfs_complete_multipart(
    namespace: str,
    name: str,
    request: Request,
    upload_id: str = None,
    ticket: str | None = Query(None),
    user: User | None = Depends(get_optional_user),
):
    """Complete multipart LFS upload.

    Called by client after uploading all parts to finalize S3 multipart upload.

    Args:
        namespace: Repository namespace
        name: Repository name
        request: FastAPI request with completion data
        upload_id: S3 upload ID (from URL or body)

    Returns:
        Completion result

    Request body (HuggingFace format):
        {
            "oid": "sha256_hash",
            "parts": [{"partNumber": 1, "etag": "etag1"}, ...]
        }

    Request body (Alternative format):
        {
            "oid": "sha256_hash",
            "size": file_size,
            "upload_id": "s3_upload_id",
            "parts": [{"PartNumber": 1, "ETag": "etag1"}, ...]
        }
    """
    if user is None and not ticket:
        raise _unauthorized()

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, detail={"error": f"Invalid completion request: {e}"})

    oid = body.get("oid")
    size = body.get("size")

    # Get upload_id from URL path or body
    if not upload_id:
        upload_id = body.get("upload_id")

    parts = body.get("parts", [])

    if not oid or not upload_id or not parts:
        raise HTTPException(
            400,
            detail={
                "error": "Missing required fields: oid, upload_id, parts",
                "received": {
                    "oid": bool(oid),
                    "upload_id": bool(upload_id),
                    "parts": bool(parts),
                },
            },
        )

    if not is_valid_oid(oid):
        raise HTTPException(400, detail={"error": "Invalid oid"})

    _authorize_followup(namespace, name, user, ticket, "complete", oid, upload_id)

    # Normalize parts format (HuggingFace uses partNumber/etag, S3 uses PartNumber/ETag)
    normalized_parts = []
    for part in parts:
        # Support both formats
        part_number = part.get("PartNumber") or part.get("partNumber")
        etag = part.get("ETag") or part.get("etag")

        if not part_number or not etag:
            raise HTTPException(
                400,
                detail={
                    "error": f"Invalid part format: {part}",
                    "expected": "PartNumber/partNumber and ETag/etag",
                },
            )

        normalized_parts.append({"PartNumber": int(part_number), "ETag": etag})

    lfs_key = get_lfs_key(oid)

    try:
        # Complete the multipart upload on S3
        logger.info(
            f"Completing multipart upload for {oid[:8]}: "
            f"{len(parts)} parts, upload_id={upload_id}"
        )

        # Debug: Log first and last few parts
        if len(normalized_parts) > 0:
            logger.debug(
                f"Parts sample for {oid[:8]}: "
                f"first={normalized_parts[0]}, last={normalized_parts[-1]}, total={len(normalized_parts)}"
            )

        result = await complete_multipart_upload(
            bucket=cfg.s3.bucket,
            key=lfs_key,
            upload_id=upload_id,
            parts=normalized_parts,
        )

        # Verify the completed object
        metadata = await get_object_metadata(cfg.s3.bucket, lfs_key)

        if size and metadata["size"] != size:
            logger.error(
                f"Size mismatch after multipart completion: "
                f"expected {size}, got {metadata['size']}"
            )
            raise HTTPException(400, detail={"error": "Size mismatch after upload"})

        logger.success(
            f"Multipart upload completed for {oid[:8]}: "
            f"size={metadata['size']}, etag={metadata['etag']}"
        )

        return {
            "message": "Multipart upload completed successfully",
            "size": metadata["size"],
            "etag": metadata["etag"],
        }

    except Exception as e:
        logger.exception(
            f"Failed to complete multipart upload for {oid[:8]} "
            f"(upload_id={upload_id})",
            e,
        )
        raise HTTPException(
            500, detail={"error": f"Failed to complete multipart upload: {str(e)}"}
        )


@router.post("/api/{namespace}/{name}.git/info/lfs/verify")
async def lfs_verify(
    namespace: str,
    name: str,
    request: Request,
    ticket: str | None = Query(None),
    user: User | None = Depends(get_optional_user),
):
    """Verify LFS upload completion.

    Called by client after successful upload to confirm the file.
    Supports both single-part and multipart uploads.

    Args:
        namespace: Repository namespace
        name: Repository name
        request: FastAPI request with verification data

    Returns:
        Verification result

    Request body (single-part):
        {
            "oid": "sha256_hash",
            "size": file_size
        }

    Request body (multipart):
        {
            "oid": "sha256_hash",
            "size": file_size,
            "upload_id": "s3_upload_id",
            "parts": [{"PartNumber": 1, "ETag": "etag1"}, ...]
        }
    """
    if user is None and not ticket:
        raise _unauthorized()

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, detail={"error": f"Invalid verification request: {e}"})

    oid = body.get("oid")
    size = body.get("size")
    upload_id = body.get("upload_id")
    parts = body.get("parts")

    if not oid:
        raise HTTPException(400, detail={"error": "Missing OID"})
    if not is_valid_oid(oid):
        raise HTTPException(400, detail={"error": "Invalid oid"})

    # A verify ticket only covers checking an object; completing a multipart
    # upload from here needs a signed-in writer (clients use ``complete``).
    completes_upload = bool(upload_id and parts)
    _authorize_followup(
        namespace,
        name,
        user,
        None if completes_upload else ticket,
        "verify",
        oid,
    )

    lfs_key = get_lfs_key(oid)

    # If this is a multipart upload, complete it first
    if upload_id and parts:
        try:
            logger.info(
                f"Completing multipart upload during verify for {oid[:8]}: "
                f"{len(parts)} parts, upload_id={upload_id}"
            )

            await complete_multipart_upload(
                bucket=cfg.s3.bucket, key=lfs_key, upload_id=upload_id, parts=parts
            )

            logger.success(f"Multipart upload completed for {oid[:8]}")

        except Exception as e:
            logger.exception(
                f"Failed to complete multipart upload for {oid[:8]} "
                f"(upload_id={upload_id})",
                e,
            )
            raise HTTPException(
                500,
                detail={"error": f"Failed to complete multipart upload: {str(e)}"},
            )

    # Verify object exists in S3
    if not await object_exists(cfg.s3.bucket, lfs_key):
        raise HTTPException(404, detail={"error": "Object not found in storage"})

    # Optionally verify size
    if size:
        try:
            metadata = await get_object_metadata(cfg.s3.bucket, lfs_key)
            if metadata["size"] != size:
                logger.error(
                    f"Size mismatch for {oid[:8]}: expected {size}, got {metadata['size']}"
                )
                raise HTTPException(400, detail={"error": "Size mismatch"})
        except HTTPException:
            raise
        except Exception as e:
            # If metadata retrieval fails, still accept the verification
            logger.warning(f"Failed to verify size for {oid[:8]}: {e}")

    return {"message": "Object verified successfully"}
