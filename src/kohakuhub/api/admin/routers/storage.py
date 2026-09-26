"""S3 storage browser endpoints for admin API."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig
from fastapi import APIRouter, Depends, HTTPException

from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.config import cfg
from kohakuhub.db_operations import (
    cleanup_expired_confirmation_tokens,
    consume_confirmation_token,
    create_confirmation_token,
)
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import (
    enqueue_purge,
    find_orphan_lakefs_repositories,
    lakefs_repo_in_use,
)
from kohakuhub.utils.lakefs import get_lakefs_client
from kohakuhub.utils.s3 import delete_objects_with_prefix, get_s3_client
from kohakuhub.api.admin.utils import verify_admin_token

logger = get_logger("ADMIN")
router = APIRouter()


@router.get("/storage/debug")
async def debug_s3_config(
    _admin: bool = Depends(verify_admin_token),
):
    """Debug S3 configuration and test connectivity.

    Returns diagnostic information about S3 setup.
    """

    def _debug():
        s3 = get_s3_client()

        info = {
            "endpoint": cfg.s3.endpoint,
            "bucket": cfg.s3.bucket,
            "region": cfg.s3.region,
            "force_path_style": cfg.s3.force_path_style,
        }

        # Test 1: Can we access the bucket?
        try:
            head_response = s3.head_bucket(Bucket=cfg.s3.bucket)
            info["bucket_accessible"] = True
            info["head_bucket_response"] = str(
                head_response.get("ResponseMetadata", {})
            )
        except Exception as e:
            info["bucket_accessible"] = False
            info["head_bucket_error"] = str(e)

        # Test 2: Try list with different parameters
        for test_name, params in [
            ("Standard", {"Bucket": cfg.s3.bucket, "MaxKeys": 10}),
            (
                "With Delimiter",
                {"Bucket": cfg.s3.bucket, "Delimiter": "/", "MaxKeys": 10},
            ),
            ("No MaxKeys", {"Bucket": cfg.s3.bucket}),
        ]:
            try:
                response = s3.list_objects_v2(**params)
                result = {
                    "success": True,
                    "response_keys": list(response.keys()),
                    "key_count": response.get("KeyCount", "N/A"),
                    "contents_count": len(response.get("Contents", [])),
                    "common_prefixes_count": len(response.get("CommonPrefixes", [])),
                }
                if response.get("Contents"):
                    result["sample_keys"] = [
                        obj["Key"] for obj in response.get("Contents", [])[:3]
                    ]
                info[f"test_{test_name}"] = result
            except Exception as e:
                info[f"test_{test_name}"] = {"success": False, "error": str(e)}

        return info

    result = await run_in_s3_executor(_debug)
    logger.info(f"S3 Debug Info: {result}")

    return result


@router.get("/storage/buckets")
async def list_s3_buckets(
    _admin: bool = Depends(verify_admin_token),
):
    """List S3 buckets and their sizes.

    Args:
        _admin: Admin authentication (dependency)

    Returns:
        List of buckets with sizes
    """

    def _list_buckets():
        s3 = get_s3_client()

        try:
            buckets = s3.list_buckets()
            logger.info(f"list_buckets() response: {buckets}")
        except Exception as e:
            logger.error(f"Failed to list buckets: {e}")
            # For R2/path-style, list_buckets might not work
            # Return configured bucket as fallback
            return [
                {
                    "name": cfg.s3.bucket,
                    "creation_date": "N/A",
                    "total_size": 0,
                    "object_count": 0,
                    "note": "Using configured bucket (list_buckets not supported)",
                }
            ]

        bucket_list = buckets.get("Buckets", [])
        logger.info(f"Found {len(bucket_list)} buckets")

        # If no buckets returned (R2 path-style issue), use configured bucket
        if not bucket_list:
            logger.warning("list_buckets returned empty, using configured bucket")
            return [
                {
                    "name": cfg.s3.bucket,
                    "creation_date": "N/A",
                    "total_size": 0,
                    "object_count": 0,
                    "note": "Using configured bucket",
                }
            ]

        bucket_info = []
        for bucket in bucket_list:
            bucket_name = bucket["Name"]

            # Get bucket size (sum of all objects)
            try:
                total_size = 0
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket_name):
                    for obj in page.get("Contents", []):
                        total_size += obj.get("Size", 0)

                object_count = sum(
                    len(page.get("Contents", []))
                    for page in paginator.paginate(Bucket=bucket_name)
                )

                bucket_info.append(
                    {
                        "name": bucket_name,
                        "creation_date": bucket["CreationDate"].isoformat(),
                        "total_size": total_size,
                        "object_count": object_count,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to get size for bucket {bucket_name}: {e}")
                bucket_info.append(
                    {
                        "name": bucket_name,
                        "creation_date": bucket["CreationDate"].isoformat(),
                        "total_size": 0,
                        "object_count": 0,
                        "error": str(e),
                    }
                )

        return bucket_info

    buckets = await run_in_s3_executor(_list_buckets)

    return {"buckets": buckets}


@router.get("/storage/objects")
@router.get("/storage/objects/{bucket}")
async def list_s3_objects(
    bucket: str = "",
    prefix: str = "",
    limit: int = 1000,
    _admin: bool = Depends(verify_admin_token),
):
    """List S3 objects in configured bucket or specified bucket.

    Args:
        bucket: Bucket name (empty = use configured bucket)
        prefix: Key prefix filter
        limit: Maximum objects to return
        _admin: Admin authentication (dependency)

    Returns:
        List of S3 objects
    """

    # Use configured bucket if not specified
    bucket_name = bucket if bucket else cfg.s3.bucket

    logger.info(
        f"Listing S3 objects: bucket={bucket_name}, prefix={prefix}, limit={limit}"
    )
    logger.info(f"S3 endpoint: {cfg.s3.endpoint}")
    logger.info(f"S3 bucket config: {cfg.s3.bucket}")

    def _list_objects():
        # Parse endpoint URL to extract path (for R2 with path-in-endpoint)
        parsed = urlparse(cfg.s3.endpoint)
        endpoint_path = parsed.path.strip("/")  # e.g., "sizigi-deepghs-grant"

        # Determine actual bucket, endpoint, and prefix
        if endpoint_path:
            # Endpoint has path - first component is bucket, rest is base prefix
            path_parts = endpoint_path.split("/")
            root_endpoint = f"{parsed.scheme}://{parsed.netloc}"
            actual_bucket = path_parts[0]  # "sizigi-deepghs-grant"

            # Combine remaining path parts + configured bucket + user prefix
            base_prefix_parts = path_parts[1:] + [bucket_name]  # ["hub-storage"]
            base_prefix = "/".join(base_prefix_parts)

            # Add user-requested prefix
            if prefix:
                actual_prefix = f"{base_prefix}/{prefix}"
            else:
                actual_prefix = f"{base_prefix}/" if base_prefix else ""

            logger.info(f"Endpoint path detected: '{endpoint_path}'")
            logger.info(f"Root endpoint: {root_endpoint}")
            logger.info(f"Actual bucket: {actual_bucket}")
            logger.info(f"Base prefix: {base_prefix}")
            logger.info(f"Final prefix: {actual_prefix}")

            # Create client with root endpoint
            s3_config = {}
            if cfg.s3.force_path_style:
                s3_config["addressing_style"] = "path"

            boto_config = BotoConfig(signature_version="s3v4", s3=s3_config)

            s3 = boto3.client(
                "s3",
                endpoint_url=root_endpoint,
                aws_access_key_id=cfg.s3.access_key,
                aws_secret_access_key=cfg.s3.secret_key,
                region_name=cfg.s3.region,
                config=boto_config,
            )
        else:
            # Standard S3/MinIO - use configured client as-is
            actual_bucket = bucket_name
            actual_prefix = prefix
            s3 = get_s3_client()
            logger.info(
                f"Standard S3 endpoint - bucket: {actual_bucket}, prefix: {actual_prefix}"
            )

        try:
            logger.info(
                f"Calling list_objects_v2(Bucket='{actual_bucket}', Prefix='{actual_prefix}', MaxKeys={limit})"
            )

            response = s3.list_objects_v2(
                Bucket=actual_bucket,
                Prefix=actual_prefix,
                MaxKeys=limit,
            )

            # Log ResponseMetadata
            metadata = response.get("ResponseMetadata", {})
            logger.info(
                f"ResponseMetadata HTTPStatusCode: {metadata.get('HTTPStatusCode')}"
            )
            logger.info(f"ResponseMetadata RequestId: {metadata.get('RequestId')}")

            # Log all response fields
            logger.info(f"Response keys: {list(response.keys())}")
            for key in ["Name", "Prefix", "KeyCount", "MaxKeys", "IsTruncated"]:
                if key in response:
                    logger.info(f"{key}: {response[key]}")

            contents = response.get("Contents", [])
            logger.info(f"Contents count: {len(contents)}")

            if contents:
                logger.success(f"Found {len(contents)} objects!")
                logger.info(f"First 3 keys: {[obj['Key'] for obj in contents[:3]]}")

            # Strip base prefix from keys for frontend display
            # So frontend sees relative paths from their perspective
            strip_prefix = f"{base_prefix}/" if endpoint_path and base_prefix else ""
            logger.info(
                f"Stripping prefix '{strip_prefix}' from object keys for frontend"
            )

            objects = []
            for obj in contents:
                original_key = obj["Key"]
                # Remove base prefix to show relative path
                display_key = (
                    original_key[len(strip_prefix) :]
                    if strip_prefix and original_key.startswith(strip_prefix)
                    else original_key
                )

                objects.append(
                    {
                        "key": display_key,  # Relative path for frontend
                        "full_key": original_key,  # Full S3 key
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                        "storage_class": obj.get("StorageClass", "STANDARD"),
                    }
                )

            if objects:
                logger.info(
                    f"Sample display keys: {[obj['key'] for obj in objects[:3]]}"
                )

            return {
                "objects": objects,
                "bucket": bucket_name,
                "is_truncated": response.get("IsTruncated", False),
                "key_count": len(objects),
            }
        except Exception as e:
            logger.error(f"Failed to list objects: {e}")
            logger.exception("Full exception:", e)
            raise HTTPException(500, detail={"error": str(e)})

    result = await run_in_s3_executor(_list_objects)

    logger.info(f"Returning {result['key_count']} objects to client")
    return result


# ========== S3 Storage Management Operations (Admin Only) ==========

# In-memory store for delete confirmations (TTL: 60 seconds)
_delete_confirmations = {}


@router.delete("/storage/objects/{key:path}")
async def delete_s3_object(
    key: str,
    _admin: bool = Depends(verify_admin_token),
):
    """Delete a single S3 object.

    Args:
        key: Full S3 object key (path parameter, auto URL-decoded)
        _admin: Admin authentication

    Returns:
        Success message
    """

    def _delete():
        s3 = get_s3_client()
        s3.delete_object(Bucket=cfg.s3.bucket, Key=key)
        return {"deleted": 1}

    result = await run_in_s3_executor(_delete)
    logger.warning(f"Admin deleted S3 object: {key}")

    return {"success": True, "message": f"Object deleted: {key}"}


@router.post("/storage/prefix/prepare-delete")
async def prepare_delete_prefix(
    prefix: str,
    _admin: bool = Depends(verify_admin_token),
):
    """Step 1: Prepare prefix deletion (generate confirmation token).

    Returns estimated object count and confirmation token.
    Token expires in 60 seconds.
    Stored in database to work across multiple workers.

    Args:
        prefix: S3 prefix to delete
        _admin: Admin authentication

    Returns:
        Confirmation token, prefix, estimated count, expiration
    """

    # Handle R2 path-in-endpoint (same logic as list_objects)
    parsed = urlparse(cfg.s3.endpoint)
    endpoint_path = parsed.path.strip("/")

    if endpoint_path:
        # Endpoint has path - need to add base prefix
        path_parts = endpoint_path.split("/")
        actual_bucket = path_parts[0]
        base_prefix_parts = path_parts[1:] + [cfg.s3.bucket]
        base_prefix = "/".join(base_prefix_parts)
        actual_prefix = f"{base_prefix}/{prefix}" if prefix else f"{base_prefix}/"
    else:
        # Standard S3 - use prefix as-is
        actual_bucket = cfg.s3.bucket
        actual_prefix = prefix

    logger.info(f"Counting objects: bucket={actual_bucket}, prefix={actual_prefix}")

    # Count objects with prefix
    def _count():
        # Parse endpoint for R2 support
        if endpoint_path:
            root_endpoint = f"{parsed.scheme}://{parsed.netloc}"
            s3_config = {}
            if cfg.s3.force_path_style:
                s3_config["addressing_style"] = "path"
            boto_config = BotoConfig(signature_version="s3v4", s3=s3_config)
            s3 = boto3.client(
                "s3",
                endpoint_url=root_endpoint,
                aws_access_key_id=cfg.s3.access_key,
                aws_secret_access_key=cfg.s3.secret_key,
                region_name=cfg.s3.region,
                config=boto_config,
            )
        else:
            s3 = get_s3_client()

        paginator = s3.get_paginator("list_objects_v2")
        count = 0
        for page in paginator.paginate(Bucket=actual_bucket, Prefix=actual_prefix):
            count += len(page.get("Contents", []))
        return count

    estimated = await run_in_s3_executor(_count)

    # Create confirmation token in database (works across workers)
    # Store ACTUAL prefix (with base prefix if needed) for deletion
    conf_token = create_confirmation_token(
        action_type="delete_s3_prefix",
        action_data={
            "display_prefix": prefix,  # What frontend sees
            "actual_prefix": actual_prefix,  # What S3 sees
            "actual_bucket": actual_bucket,  # Actual bucket name
            "estimated_count": estimated,
        },
        ttl_seconds=60,
    )

    # Cleanup expired tokens (async, non-blocking)
    cleanup_expired_confirmation_tokens()

    logger.warning(f"Admin prepared delete for prefix: {prefix} ({estimated} objects)")

    return {
        "confirm_token": conf_token.token,
        "prefix": prefix,
        "estimated_objects": estimated,
        "expires_in": 60,
    }


@router.delete("/storage/prefix")
async def delete_s3_prefix(
    prefix: str,
    confirm_token: str,
    _admin: bool = Depends(verify_admin_token),
):
    """Step 2: Delete all objects under prefix (requires confirmation token).

    Args:
        prefix: S3 prefix to delete
        confirm_token: Token from /prepare-delete
        _admin: Admin authentication

    Returns:
        Success message with deleted count
    """
    # Validate and consume confirmation token from database
    action_data = consume_confirmation_token(confirm_token)

    if not action_data:
        raise HTTPException(400, detail="Invalid or expired confirmation token")

    # Verify display prefix matches (what user requested)
    if action_data.get("display_prefix") != prefix:
        raise HTTPException(400, detail="Prefix mismatch with confirmation token")

    # Use actual S3 prefix and bucket from token (handles R2 path-in-endpoint)
    actual_prefix = action_data.get("actual_prefix")
    actual_bucket = action_data.get("actual_bucket")

    logger.info(f"Deleting: bucket={actual_bucket}, prefix={actual_prefix}")

    # Delete objects - handle R2 path-in-endpoint
    parsed = urlparse(cfg.s3.endpoint)
    endpoint_path = parsed.path.strip("/")

    def _delete_with_r2_support():
        # Create appropriate S3 client based on endpoint type
        if endpoint_path:
            # R2 with path - use root endpoint
            root_endpoint = f"{parsed.scheme}://{parsed.netloc}"
            s3_config = {}
            if cfg.s3.force_path_style:
                s3_config["addressing_style"] = "path"
            boto_config = BotoConfig(signature_version="s3v4", s3=s3_config)
            s3 = boto3.client(
                "s3",
                endpoint_url=root_endpoint,
                aws_access_key_id=cfg.s3.access_key,
                aws_secret_access_key=cfg.s3.secret_key,
                region_name=cfg.s3.region,
                config=boto_config,
            )
        else:
            # Standard S3/MinIO
            s3 = get_s3_client()

        # Delete objects
        paginator = s3.get_paginator("list_objects_v2")
        deleted_count = 0

        # List and delete in batches
        for page in paginator.paginate(Bucket=actual_bucket, Prefix=actual_prefix):
            if "Contents" not in page or not page["Contents"]:
                continue

            # Delete batch (max 1000 objects)
            delete_keys = [{"Key": obj["Key"]} for obj in page["Contents"]]
            response = s3.delete_objects(
                Bucket=actual_bucket,
                Delete={"Objects": delete_keys, "Quiet": True},
            )

            deleted_count += len(response.get("Deleted", []))

            if "Errors" in response:
                for error in response["Errors"]:
                    logger.warning(
                        f"Failed to delete {error['Key']}: {error['Message']}"
                    )

        return deleted_count

    deleted_count = await run_in_s3_executor(_delete_with_r2_support)

    logger.warning(f"Admin deleted S3 prefix: {prefix} ({deleted_count} objects)")

    return {"success": True, "deleted_count": deleted_count, "prefix": prefix}


@router.get("/storage/orphans")
async def list_orphan_lakefs_repositories(_admin: bool = Depends(verify_admin_token)):
    """LakeFS repositories that no repository row points at.

    Read-only. They are left behind by repositories deleted before storage
    cleanup was scheduled on deletion (#109), or by creates that failed
    halfway. Each entry says whether a purge is already queued or running.
    """
    orphans = await find_orphan_lakefs_repositories()
    return {"orphans": orphans, "count": len(orphans)}


@router.post("/storage/orphans/{lakefs_repo}/purge")
async def purge_orphan_lakefs_repository(
    lakefs_repo: str, _admin: bool = Depends(verify_admin_token)
):
    """Schedule the deletion of an orphaned LakeFS repository and its S3 prefix.

    Refused for a LakeFS repository that a repository row points at. The
    purge runs as a background task; the task checks again before deleting.
    """
    if lakefs_repo_in_use(lakefs_repo):
        raise HTTPException(
            409,
            detail={"error": f"LakeFS repository {lakefs_repo} backs a repository; not an orphan"},
        )
    if not await get_lakefs_client().repository_exists(lakefs_repo):
        raise HTTPException(404, detail={"error": f"LakeFS repository not found: {lakefs_repo}"})
    task_id = enqueue_purge(lakefs_repo, f"orphan:{lakefs_repo}")
    logger.warning(f"Admin scheduled the purge of orphaned LakeFS repository {lakefs_repo}")
    return {"lakefs_repo": lakefs_repo, "task_id": task_id, "already_pending": task_id is None}
