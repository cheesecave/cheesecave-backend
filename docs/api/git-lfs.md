---
title: Git LFS API
description: Large File Storage protocol for efficient handling of large files
icon: i-carbon-data-blob
---

# Git LFS API

Git LFS (Large File Storage) protocol for handling large files efficiently with direct S3 uploads/downloads.

---

## Overview

**When to use LFS:**
- Files ≥ LFS threshold (configurable per repo, default 5MB)
- Files matching LFS suffix rules (`.safetensors`, `.bin`, `.gguf`, etc.)
- 32 server-wide default suffixes always use LFS

**Benefits:**
- Direct S3 uploads (no server proxy)
- Content deduplication (same file = same storage)
- Multipart uploads for files >100MB
- Parallel part uploads for faster transfers

---

## Batch API

### Upload/Download Batch Request

**Pattern:** `POST /{repo_type}s/{namespace}/{name}.git/info/lfs/objects/batch`

**Alternative:** `POST /{namespace}/{name}.git/info/lfs/objects/batch`

**Authentication:**
- Optional for `download` of a public repository
- Required for `download` of a private repository, and for every `upload` (write permission on the repository)

**Repository resolution** happens first, before any action or URL is produced:

| Caller | Repository missing or not visible to the caller | Visible, no write permission (`upload`) |
|---|---|---|
| Anonymous | `401` + `LFS-Authenticate` | `401` + `LFS-Authenticate` |
| Signed in | `404` (`X-Error-Code: RepoNotFound`) | `403` |

A missing repository and a private repository the caller cannot see get the same answer. Object ids must be lowercase SHA-256 hex; any other id gets a per-object `422` error.

**Request Body:**
```json
{
  "operation": "upload",
  "transfers": ["basic"],
  "objects": [
    {
      "oid": "abc123def456...",
      "size": 536870912
    }
  ],
  "hash_algo": "sha256",
  "is_browser": false
}
```

**Fields:**
- `operation`: `"upload"` or `"download"`
- `transfers`: Array of transfer types (only `"basic"` supported)
- `objects`: Array of file objects with OID (SHA256) and size
- `hash_algo`: Hash algorithm (default: `"sha256"`)
- `is_browser`: Set to `true` for browser uploads (includes Content-Type in presigned URL)

---

### Response Format

#### Single-Part Upload (< 100MB)

**Response:**
```json
{
  "transfer": "basic",
  "hash_algo": "sha256",
  "objects": [
    {
      "oid": "abc123def456...",
      "size": 52428800,
      "authenticated": true,
      "actions": {
        "upload": {
          "href": "https://s3.amazonaws.com/bucket/lfs/ab/c1/abc123...?X-Amz-...",
          "expires_at": "2025-01-20T12:00:00Z"
        },
        "verify": {
          "href": "/api/namespace/repo.git/info/lfs/verify?ticket=1790000000.9f2c...",
          "expires_at": "2025-01-20T12:00:00Z"
        }
      }
    }
  ]
}
```

**Upload Process:**
1. `PUT` to `actions.upload.href` with file content
2. `POST` to `actions.verify.href` to confirm upload

---

#### Multipart Upload (≥ 100MB)

**Response:**
```json
{
  "transfer": "basic",
  "hash_algo": "sha256",
  "objects": [
    {
      "oid": "abc123def456...",
      "size": 524288000,
      "authenticated": true,
      "actions": {
        "upload": {
          "href": "unused_for_multipart",
          "expires_at": "2025-01-20T12:00:00Z",
          "header": {
            "chunk_size": "52428800",
            "upload_id": "s3_upload_id_xxx",
            "1": "https://s3.amazonaws.com/.../uploadId=xxx&partNumber=1&...",
            "2": "https://s3.amazonaws.com/.../uploadId=xxx&partNumber=2&...",
            "3": "https://s3.amazonaws.com/.../uploadId=xxx&partNumber=3&...",
            "...": "..."
          }
        },
        "verify": {
          "href": "/api/namespace/repo.git/info/lfs/verify?ticket=1790000000.9f2c...",
          "expires_at": "2025-01-20T12:00:00Z"
        }
      }
    }
  ]
}
```

**Multipart Upload Process:**
1. Split file into chunks (size from `chunk_size` header)
2. `PUT` each chunk to `header.{part_number}` URL in parallel
3. Collect ETags from each part upload
4. `POST` to `/lfs/complete` endpoint with ETags
5. `POST` to `actions.verify.href` to confirm

**Chunk Size:**
- Default: 50MB (configurable via `KOHAKU_HUB_LFS_MULTIPART_CHUNK_SIZE_BYTES`)
- Minimum: 5MB (S3 requirement, except last part)
- Maximum parts: 10,000 (S3 limit)

---

#### Download Response

**Response:**
```json
{
  "transfer": "basic",
  "hash_algo": "sha256",
  "objects": [
    {
      "oid": "abc123def456...",
      "size": 536870912,
      "authenticated": true,
      "actions": {
        "download": {
          "href": "https://s3.amazonaws.com/bucket/lfs/ab/c1/abc123...?X-Amz-...",
          "expires_at": "2025-01-20T12:00:00Z"
        }
      }
    }
  ]
}
```

**Download Process:**
1. `GET` from `actions.download.href`
2. File downloaded directly from S3

---

#### Existing File (Deduplication)

**Response:**
```json
{
  "transfer": "basic",
  "hash_algo": "sha256",
  "objects": [
    {
      "oid": "abc123def456...",
      "size": 536870912,
      "authenticated": true
    }
  ]
}
```

**No `actions` field = file already exists, skip upload**

---

#### Not Found Error

**Response:**
```json
{
  "transfer": "basic",
  "hash_algo": "sha256",
  "objects": [
    {
      "oid": "abc123def456...",
      "size": 536870912,
      "authenticated": true,
      "error": {
        "code": 404,
        "message": "Object not found in storage"
      }
    }
  ]
}
```

---

## Multipart Complete

### Complete Multipart Upload

**Pattern:** `POST /api/{namespace}/{name}.git/info/lfs/complete/{upload_id}`

**Alternative:** `POST /api/{namespace}/{name}.git/info/lfs/complete`

**Authentication:** one of the following, otherwise `401` with an `LFS-Authenticate` challenge:

- the signed `ticket` query parameter that the batch response put into this URL (use the `href` exactly as returned; clients that send no credentials on this call, such as `huggingface_hub` and the web UI, rely on it). A ticket is bound to the repository, the object id (and, for `complete`, the upload id) and expires with the part URLs;
- a signed-in caller (session cookie or Bearer token) with write permission on the repository named in the URL. A caller who can read but not write gets `403`; one who cannot see the repository gets `404`.

**Purpose:** Signal S3 to assemble uploaded parts into final object

**Request Body:**
```json
{
  "oid": "abc123def456...",
  "size": 524288000,
  "upload_id": "s3_upload_id_xxx",
  "parts": [
    {
      "PartNumber": 1,
      "ETag": "etag_from_part_1_upload"
    },
    {
      "partNumber": 2,
      "etag": "etag_from_part_2_upload"
    },
    {
      "PartNumber": 3,
      "ETag": "etag_from_part_3_upload"
    }
  ]
}
```

**Field Notes:**
- `PartNumber` or `partNumber` (case-insensitive)
- `ETag` or `etag` (case-insensitive)
- ETags obtained from part upload responses

**Response:**
```json
{
  "success": true,
  "message": "Multipart upload completed",
  "size": 524288000,
  "etag": "final_s3_etag"
}
```

**Status Codes:**
- `200 OK` - Success
- `400 Bad Request` - Missing fields, size mismatch, or invalid parts
- `500 Internal Server Error` - S3 completion failed

---

## Verify

### Verify Upload

**Pattern:** `POST /api/{namespace}/{name}.git/info/lfs/verify`

**Authentication:** one of the following, otherwise `401` with an `LFS-Authenticate` challenge:

- the signed `ticket` query parameter that the batch response put into this URL (use the `href` exactly as returned; clients that send no credentials on this call, such as `huggingface_hub` and the web UI, rely on it). A ticket is bound to the repository, the object id (and, for `complete`, the upload id) and expires with the part URLs;
- a signed-in caller (session cookie or Bearer token) with write permission on the repository named in the URL. A caller who can read but not write gets `403`; one who cannot see the repository gets `404`.

**Purpose:** Verify file was uploaded correctly and exists in storage

`verify` also accepts the multipart form below, which completes the upload first. A ticket does not cover that form; it needs the signed-in writer.

**Request Body (Single-Part):**
```json
{
  "oid": "abc123def456...",
  "size": 52428800
}
```

**Request Body (Multipart):**
```json
{
  "oid": "abc123def456...",
  "size": 524288000,
  "upload_id": "s3_upload_id_xxx",
  "parts": [
    {"PartNumber": 1, "ETag": "etag1"},
    {"PartNumber": 2, "ETag": "etag2"}
  ]
}
```

**Verification Steps:**
1. Check file exists in S3 at `lfs/{oid[:2]}/{oid[2:4]}/{oid}`
2. Verify size matches
3. For multipart: Complete upload if not already done

**Response:**
```json
{
  "success": true,
  "message": "Object verified",
  "oid": "abc123def456...",
  "size": 52428800
}
```

**Status Codes:**
- `200 OK` - Verified successfully
- `400 Bad Request` - Size mismatch
- `404 Not Found` - Object not found in storage
- `500 Internal Server Error` - Verification failed

---

## LFS Threshold & Rules

### Repository-Specific Settings

Files use LFS if they meet **either** condition:
1. **Size:** `file_size ≥ lfs_threshold_bytes`
2. **Suffix:** File extension matches `lfs_suffix_rules`

**Configuration Levels:**
- **Server default:** `KOHAKU_HUB_LFS_THRESHOLD_BYTES=5000000` (5MB)
- **Repository override:** Per-repo custom threshold and suffix rules
- **Server suffix defaults:** 32 built-in suffixes always use LFS

**Server Default Suffixes (Always LFS):**
- ML Models: `.safetensors`, `.bin`, `.pt`, `.pth`, `.ckpt`, `.onnx`, `.pb`, `.h5`, `.tflite`, `.gguf`, `.ggml`, `.msgpack`
- Archives: `.zip`, `.tar`, `.gz`, `.bz2`, `.xz`, `.7z`, `.rar`
- Data: `.npy`, `.npz`, `.arrow`, `.parquet`
- Media: `.mp4`, `.avi`, `.mkv`, `.mov`, `.wav`, `.mp3`, `.flac`
- Images: `.tiff`, `.tif`

**Example:**
- `model.safetensors` (100KB) → Uses LFS (suffix rule)
- `config.json` (1KB) → Regular (< threshold, no suffix match)
- `data.bin` (10MB) → Uses LFS (suffix rule + size)
- `large_file.txt` (20MB) → Uses LFS (size only)

### Get Repository LFS Settings

**Pattern:** `GET /api/{repo_type}s/{namespace}/{name}/settings/lfs`

**Authentication:** Required (repo owner or admin)

**Response:**
```json
{
  "lfs_threshold_bytes": 10000000,
  "lfs_keep_versions": 10,
  "lfs_suffix_rules": [".safetensors", ".custom"],
  "lfs_threshold_bytes_effective": 10000000,
  "lfs_threshold_bytes_source": "repository",
  "lfs_keep_versions_effective": 10,
  "lfs_keep_versions_source": "repository",
  "lfs_suffix_rules_effective": [".safetensors", ".bin", "...", ".custom"],
  "lfs_suffix_rules_source": "merged",
  "server_defaults": {
    "lfs_threshold_bytes": 5000000,
    "lfs_keep_versions": 5,
    "lfs_suffix_rules_default": [".safetensors", ".bin", "..."]
  }
}
```

### Update Repository LFS Settings

**Pattern:** `PUT /api/{repo_type}s/{namespace}/{name}/settings`

**Request Body:**
```json
{
  "lfs_threshold_bytes": 10000000,
  "lfs_keep_versions": 10,
  "lfs_suffix_rules": [".safetensors", ".custom"]
}
```

**Notes:**
- `null` value = inherit server default
- `lfs_suffix_rules` adds to (not replaces) server defaults
- `lfs_keep_versions` controls garbage collection

---

## Storage & Deduplication

### LFS Object Storage

**S3 Path:** `s3://{bucket}/lfs/{sha256[:2]}/{sha256[2:4]}/{sha256}`

**Example:**
- OID: `abc123def456...`
- Path: `lfs/ab/c1/abc123def456...`

**Deduplication:**
- Same content = same SHA256 = same S3 object
- Multiple repos can reference same LFS object
- Saves storage space automatically

### Garbage Collection

**When objects are deleted:**
- Files deleted from repository
- File replaced with new version
- Repository deleted
- Based on `lfs_keep_versions` setting

**LFS Keep Versions:**
- Default: 5 versions per file path
- Configurable per repository
- Older versions auto-deleted on new uploads
- Manual GC via admin API

---

## Client Examples

### Upload with huggingface_hub

```python
from huggingface_hub import HfApi

api = HfApi(endpoint="http://localhost:28080")

# Upload large file (auto-detects LFS)
api.upload_file(
    path_or_fileobj="model.safetensors",
    path_in_repo="model.safetensors",
    repo_id="username/my-model",
    repo_type="model",
    token="your_token"
)
```

### Manual LFS Upload (Multipart)

```python
import requests
import hashlib

# 1. Calculate SHA256
with open("large_file.bin", "rb") as f:
    sha256 = hashlib.sha256(f.read()).hexdigest()
    file_size = f.tell()

# 2. Request batch
batch_req = {
    "operation": "upload",
    "transfers": ["basic"],
    "objects": [{"oid": sha256, "size": file_size}],
    "hash_algo": "sha256"
}

batch_resp = requests.post(
    "http://localhost:28080/username/repo.git/info/lfs/objects/batch",
    json=batch_req,
    headers={"Authorization": "Bearer your_token"}
).json()

obj = batch_resp["objects"][0]

# 3. Check if multipart
if "chunk_size" in obj["actions"]["upload"].get("header", {}):
    # Multipart upload
    header = obj["actions"]["upload"]["header"]
    chunk_size = int(header["chunk_size"])
    upload_id = header["upload_id"]

    # Upload parts
    parts = []
    with open("large_file.bin", "rb") as f:
        part_num = 1
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break

            # Upload part
            part_url = header[str(part_num)]
            resp = requests.put(part_url, data=chunk)
            etag = resp.headers["ETag"].strip('"')
            parts.append({"PartNumber": part_num, "ETag": etag})
            part_num += 1

    # Complete multipart
    complete_resp = requests.post(
        f"http://localhost:28080/api/username/repo.git/info/lfs/complete/{upload_id}",
        json={"oid": sha256, "size": file_size, "upload_id": upload_id, "parts": parts}
    )

    # Verify
    verify_resp = requests.post(
        obj["actions"]["verify"]["href"],
        json={"oid": sha256, "size": file_size, "upload_id": upload_id, "parts": parts}
    )
else:
    # Single-part upload
    with open("large_file.bin", "rb") as f:
        requests.put(obj["actions"]["upload"]["href"], data=f)

    # Verify
    requests.post(
        obj["actions"]["verify"]["href"],
        json={"oid": sha256, "size": file_size}
    )
```

---

## Error Handling

**413 Payload Too Large:**
```json
{
  "error": "Storage quota exceeded",
  "message": "You have used 9.5 GB of your 10 GB quota"
}
```

**404 Object Not Found:**
```json
{
  "objects": [
    {
      "oid": "abc123...",
      "size": 12345,
      "error": {
        "code": 404,
        "message": "Object not found in storage"
      }
    }
  ]
}
```

**400 Invalid Request:**
```json
{
  "error": "Size mismatch: expected 524288000, got 524287999"
}
```

---

## Performance Tips

**For uploaders:**
- Use multipart for files >100MB
- Upload parts in parallel (up to 10 concurrent)
- Increase chunk size for faster uploads (max 100MB)
- Retry failed parts (don't restart entire upload)

**For downloaders:**
- Use HTTP range requests for partial downloads
- Resume interrupted downloads
- Parallel downloads for multiple files
- Cache downloaded LFS objects locally

**Configuration:**
```bash
# Increase multipart threshold (default 100MB)
KOHAKU_HUB_LFS_MULTIPART_THRESHOLD_BYTES=209715200  # 200MB

# Increase chunk size (default 50MB)
KOHAKU_HUB_LFS_MULTIPART_CHUNK_SIZE_BYTES=104857600  # 100MB
```

---

## Next Steps

- [Git Protocol API](./git-protocol.md) - Git Smart HTTP
- [File Upload API](./file-upload.md) - Direct commits
- [Admin API](./admin.md) - LFS management
