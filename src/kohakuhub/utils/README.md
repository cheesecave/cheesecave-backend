# KohakuHub Utils Module

## Overview

The `utils` module provides shared infrastructure utilities for KohakuHub's storage and version control operations. It serves as the foundational layer for interacting with LakeFS (version control for data lakes) and S3-compatible object storage systems, abstracting away complexity and providing consistent interfaces for the application's API modules.

## Purpose and Functionality

This module centralizes all low-level storage and version control operations, ensuring:

- **Consistent Storage Access**: Unified interface for S3-compatible storage operations (MinIO, AWS S3, etc.)
- **Version Control Integration**: Seamless integration with LakeFS for Git-like version control over object storage
- **Async Operation Support**: All storage operations are async-compatible for high-performance concurrent access
- **Configuration Management**: Centralized configuration handling for storage endpoints and credentials

## Key Features and Capabilities

### LakeFS Integration
- REST client access for LakeFS API operations
- Repository naming conventions for HuggingFace-style repositories
- Support for branching, committing, and merging operations on data

### S3 Storage Operations
- **Presigned URLs**: Generate secure, time-limited URLs for uploads and downloads
- **Multipart Upload**: Handle large file uploads (>5GB) with multipart upload support
- **Object Management**: Check existence, retrieve metadata, copy, and delete operations
- **Batch Operations**: Efficient prefix-based operations for bulk copying and deletion
- **Async Execution**: All operations run asynchronously using dedicated executor threads

## Module Components

### `__init__.py`
Module initialization file that marks the directory as a Python package.

**Purpose**: Global utility modules infrastructure marker

### `lakefs.py` - LakeFS Client and Operations
Provides utilities for interacting with LakeFS version control system.

**Key Functions**:

- **`get_lakefs_client() -> LakeFSRestClient`**
  - Returns configured LakeFS REST client instance
  - Used throughout the application to access LakeFS API
  - Configuration loaded from application config

- **`lakefs_repo_name(repo_type: str, repo_id: str) -> str`**
  - Generates LakeFS-safe repository names from HuggingFace repository IDs
  - Converts slashes and underscores to hyphens for compatibility
  - Format: `{namespace}-{type}-{org}-{repo-name}`
  - Example: `"hf-model-openai-gpt2"` from `repo_id="openai/gpt2"`

**Integration**: Used by:
- `api/git/utils/lakefs_bridge.py` - Git LFS bridge operations
- `api/repo/routers/` - Repository CRUD, info, and tree operations
- `api/commit/routers/` - Commit history and operations
- `api/branches.py` - Branch management
- `api/files.py` - File operations
- `api/quota/util.py` - Storage quota calculations

### `s3.py` - S3 Storage Operations
Comprehensive S3-compatible storage utilities with async support.

**Configuration Functions**:

- **`get_s3_client()`**
  - Creates configured boto3 S3 client
  - Supports path-style and virtual-hosted-style addressing
  - Configures endpoint, credentials, and region from app config

- **`init_storage()`**
  - Initializes S3 bucket on application startup
  - Creates bucket if it doesn't exist
  - Handles region-specific bucket creation constraints

**Presigned URL Operations**:

- **`generate_download_presigned_url(bucket, key, expires_in=3600, filename=None) -> str`**
  - Generates time-limited download URLs
  - Optional Content-Disposition header for custom filenames
  - Returns a URL signed for, and pointing to, the public endpoint (`KOHAKU_HUB_S3_PUBLIC_ENDPOINT`)

- **`generate_upload_presigned_url(bucket, key, expires_in=3600, content_type=None, checksum_sha256=None) -> dict`**
  - Generates time-limited upload URLs with PUT method
  - Optional content type and checksum validation
  - Returns URL, expiration time, and required headers

**Multipart Upload Operations**:

- **`generate_multipart_upload_urls(bucket, key, part_count, upload_id=None, expires_in=3600) -> dict`**
  - Generates presigned URLs for each part of a multipart upload
  - Required for files >5GB per S3 specifications
  - Supports resuming uploads with existing upload_id
  - Returns upload_id, part_urls array, and expiration time

- **`complete_multipart_upload(bucket, key, upload_id, parts) -> dict`**
  - Finalizes multipart upload after all parts uploaded
  - Requires list of part numbers and ETags
  - Returns S3 completion response

- **`abort_multipart_upload(bucket, key, upload_id)`**
  - Cancels in-progress multipart upload
  - Cleans up uploaded parts

**Object Metadata Operations**:

- **`get_object_metadata(bucket, key) -> dict`**
  - Retrieves object metadata (size, etag, last_modified, content_type)
  - Uses HEAD request for efficiency
  - Raises ClientError if object not found

- **`object_exists(bucket, key) -> bool`**
  - Quick existence check for S3 objects
  - Returns True/False without raising exceptions

**Batch Operations**:

- **`delete_objects_with_prefix(bucket, prefix) -> int`**
  - Deletes all objects matching a prefix
  - Handles pagination for large object sets
  - Deletes in batches of 1000 objects (S3 limit)
  - Returns count of deleted objects
  - Raises when listing or deleting fails (`ClientError`) or the store refuses some objects (`S3DeleteFailed`),
    instead of returning the count of what it got through; running it again deletes the rest
  - Used for repository cleanup operations

**Utility Functions**:

- **`parse_s3_uri(uri) -> tuple`**
  - Parses S3 URI format (`s3://bucket/path/to/file`)
  - Returns (bucket, key) tuple
  - Validates URI format

**Integration**: Used by:
- `main.py` - Application initialization (`init_storage()`)
- `api/git/routers/lfs.py` - Git LFS upload/download operations
- `api/repo/routers/crud.py` - Repository copy/delete operations
- `api/repo/utils/gc.py` - Garbage collection and cleanup
- `api/files.py` - File download operations
- `api/commit/routers/operations.py` - Commit operations with file metadata
- `api/admin.py` - Administrative storage operations

## System Integration

The utils module acts as the infrastructure foundation for KohakuHub:

```
┌─────────────────────────────────────────┐
│         API Layer (FastAPI)             │
│  - Repository CRUD                      │
│  - Git LFS operations                   │
│  - File management                      │
│  - Commit/Branch operations             │
└─────────────────┬───────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────┐
│         Utils Module                    │
│  - lakefs.py: Version control client    │
│  - s3.py: Object storage operations     │
└─────────────┬───────────┬───────────────┘
              │           │
              ▼           ▼
┌─────────────────┐   ┌──────────────────┐
│     LakeFS      │   │   S3 Storage     │
│  (Version Ctrl) │   │  (MinIO/AWS S3)  │
└─────────────────┘   └──────────────────┘
```

### Key Integration Points

1. **Application Startup** (`main.py`)
   - Initializes S3 storage on application start
   - Ensures bucket exists before accepting requests

2. **Repository Operations** (`api/repo/`)
   - Creates LakeFS repositories with proper naming
   - Manages repository lifecycle (create, rename, delete)
   - Handles bulk copy/delete operations via S3

3. **Git LFS Bridge** (`api/git/`)
   - Generates presigned URLs for LFS pointer file uploads
   - Handles multipart uploads for large model files
   - Integrates Git LFS protocol with LakeFS storage

4. **File Operations** (`api/files.py`, `api/commit/`)
   - Retrieves file metadata and download URLs
   - Checks object existence for validation
   - Parses S3 URIs from LakeFS responses

5. **Storage Quota** (`api/quota/`)
   - Uses LakeFS client to calculate repository storage usage
   - Aggregates object sizes for quota enforcement

## Design Patterns

### Async/Sync Separation
All S3 operations follow a pattern:
- Synchronous implementation: `_operation_name_sync()`
- Async wrapper: `operation_name()` that uses `run_in_s3_executor()`

This design allows:
- Use of boto3 (synchronous library) in async context
- Proper thread pool management for I/O operations
- Non-blocking API endpoints

### Configuration Isolation
All configuration is loaded from `kohakuhub.config.cfg`:
- Endpoint URLs and credentials
- Bucket names and regions
- Path style settings

This enables:
- Easy environment switching (dev/staging/production)
- Support for different S3-compatible providers
- Centralized credential management

### Error Handling
- Storage operations log errors with context
- Graceful degradation where appropriate
- Batch operations continue on individual failures

## Dependencies

- **boto3**: AWS SDK for Python (S3 operations)
- **lakefs-sdk**: LakeFS REST API client
- **kohakuhub.config**: Application configuration
- **kohakuhub.async_utils**: Async executor management
- **kohakuhub.logger**: Structured logging

## Usage Examples

### Get LakeFS Repository Name
```python
from kohakuhub.utils.lakefs import lakefs_repo_name

repo_name = lakefs_repo_name("model", "openai/gpt-2")
# Returns: "hf-model-openai-gpt-2"
```

### Generate Download URL
```python
from kohakuhub.utils.s3 import generate_download_presigned_url

url = await generate_download_presigned_url(
    bucket="kohaku-storage",
    key="hf-model-org-repo/main/model.safetensors",
    expires_in=86400,
    filename="model.safetensors"
)
```

### Check Object Existence
```python
from kohakuhub.utils.s3 import object_exists

exists = await object_exists("kohaku-storage", "path/to/file")
if exists:
    metadata = await get_object_metadata("kohaku-storage", "path/to/file")
```

### Delete Repository Storage
```python
from kohakuhub.utils.s3 import delete_objects_with_prefix

count = await delete_objects_with_prefix(
    bucket="kohaku-storage",
    prefix="hf-model-org-repo/"
)
print(f"Deleted {count} objects")
```

## Performance Considerations

- All S3 operations run in dedicated thread pool to avoid blocking
- Batch operations use S3 pagination and handle large object counts
- Presigned URLs offload data transfer from application servers
- Multipart upload enables parallel part uploads for large files

## Security Notes

- Presigned URLs are time-limited (default 1 hour)
- S3 credentials managed through configuration, never hardcoded
- LakeFS client uses configured authentication
- Every presigned URL (download, upload, multipart part, whatever the part count) is signed for the public endpoint by one client (`_presign_client`), because SigV4 signs the `Host` the request carries; a proxy must pass `Host` through unchanged
