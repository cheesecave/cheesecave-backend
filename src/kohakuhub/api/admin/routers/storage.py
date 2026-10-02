"""S3 storage browser endpoints for admin API."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    enqueue_lfs_reconciliation,
    enqueue_purge,
    find_orphan_lakefs_repositories,
    lakefs_repo_in_use,
    lfs_reconciliation_status,
)
from kohakuhub.utils.lakefs import get_lakefs_client
from kohakuhub.utils.s3 import bucket_in_endpoint, get_s3_client
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


def _created(bucket: dict) -> str:
    created = bucket["CreationDate"]
    return created.isoformat() if created else "N/A"


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

        if bucket_in_endpoint():
            # The configured bucket is a key prefix inside the endpoint's: list
            # only it, sized within the prefix, never the whole real bucket
            bucket_list = [{"Name": cfg.s3.bucket, "CreationDate": None}]
        else:
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
                        "creation_date": _created(bucket),
                        "total_size": total_size,
                        "object_count": object_count,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to get size for bucket {bucket_name}: {e}")
                bucket_info.append(
                    {
                        "name": bucket_name,
                        "creation_date": _created(bucket),
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
    if bucket_in_endpoint() and bucket_name != cfg.s3.bucket:
        # Only the configured bucket's prefix belongs to the hub
        raise HTTPException(400, detail={"error": f"Only bucket {cfg.s3.bucket} can be listed"})

    logger.info(
        f"Listing S3 objects: bucket={bucket_name}, prefix={prefix}, limit={limit}"
    )

    def _list_objects():
        # An endpoint with a path is readdressed by the client (bucket_in_endpoint)
        s3 = get_s3_client()
        try:
            response = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix, MaxKeys=limit)
        except Exception as e:
            logger.exception("Failed to list objects", e)
            raise HTTPException(500, detail={"error": str(e)})
        objects = [
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
                "storage_class": obj.get("StorageClass", "STANDARD"),
            }
            for obj in response.get("Contents", [])
        ]
        return {
            "objects": objects,
            "bucket": bucket_name,
            "is_truncated": response.get("IsTruncated", False),
            "key_count": len(objects),
        }

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

    logger.info(f"Counting objects: bucket={cfg.s3.bucket}, prefix={prefix}")

    def _count():
        # An endpoint with a path is readdressed by the client (bucket_in_endpoint)
        paginator = get_s3_client().get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=cfg.s3.bucket, Prefix=prefix)
        return sum(len(page.get("Contents", [])) for page in pages)

    estimated = await run_in_s3_executor(_count)

    # Create confirmation token in database (works across workers)
    conf_token = create_confirmation_token(
        action_type="delete_s3_prefix",
        action_data={"display_prefix": prefix, "estimated_count": estimated},
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

    logger.info(f"Deleting: bucket={cfg.s3.bucket}, prefix={prefix}")

    def _delete():
        # An endpoint with a path is readdressed by the client (bucket_in_endpoint)
        s3 = get_s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        deleted_count = 0
        for page in paginator.paginate(Bucket=cfg.s3.bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not keys:
                continue
            response = s3.delete_objects(
                Bucket=cfg.s3.bucket, Delete={"Objects": keys, "Quiet": True}
            )
            # Quiet: S3 answers only the keys it could not delete
            errors = response.get("Errors", [])
            deleted_count += len(keys) - len(errors)
            for error in errors:
                logger.warning(f"Failed to delete {error['Key']}: {error['Message']}")
        return deleted_count

    deleted_count = await run_in_s3_executor(_delete)

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


@router.get("/storage/lfs-reconciliation")
async def get_lfs_reconciliation(_admin: bool = Depends(verify_admin_token)):
    """When LFS references were last reconciled, and the latest reconciliation task."""
    return lfs_reconciliation_status()


@router.post("/storage/lfs-reconciliation")
async def start_lfs_reconciliation(_admin: bool = Depends(verify_admin_token)):
    """Start reconciling the database with the LFS objects every branch head links.

    Idempotent and non-destructive: it only adds history rows and corrects
    file rows, so running it again changes nothing. One runs at a time.
    """
    task_id = enqueue_lfs_reconciliation()
    logger.info("Admin started the LFS reference reconciliation")
    return {"task_id": task_id, "already_pending": task_id is None}
