---
title: File Upload & Commit API
description: Direct file uploads via NDJSON commit protocol
icon: i-carbon-document-add
---

# File Upload & Commit API

Direct file upload and commit operations using HuggingFace-compatible NDJSON protocol.

---

## Preupload Check

### Check Files Before Upload

**Pattern:** `POST /{repo_type}s/{namespace}/{name}/preupload/{revision}`

**Authentication:** Required (write permission)

**Purpose:**
- Determine upload mode (regular vs LFS)
- Check for duplicate files (content deduplication)
- Validate quota before upload

**Request Body:**
```json
{
  "files": [
    {
      "path": "model.safetensors",
      "size": 5368709120,
      "sha256": "abc123def456...",
      "sample": "base64_encoded_first_512_bytes"
    },
    {
      "path": "config.json",
      "size": 512
    }
  ]
}
```

**Field Explanations:**
- `path`: File path in repository (required)
- `size`: File size in bytes (required)
- `sha256`: SHA256 hash of file (optional, enables deduplication)
- `sample`: Base64 encoded sample of file content (optional, for small files)

**Response:**
```json
{
  "files": [
    {
      "path": "model.safetensors",
      "uploadMode": "lfs",
      "shouldIgnore": false
    },
    {
      "path": "config.json",
      "uploadMode": "regular",
      "shouldIgnore": true
    }
  ]
}
```

**Upload Modes:**
- `"lfs"`: File matches LFS criteria (size ≥ threshold OR suffix match)
- `"regular"`: File is small enough for inline base64 upload

**Should Ignore:**
- `true`: File with same content already exists (skip upload)
- `false`: File is new or changed (upload required)

**Status Codes:**
- `200 OK` - Success
- `400 Bad Request` - Invalid payload
- `404 Not Found` - Repository not found
- `413 Payload Too Large` - Quota exceeded

---

## Commit Operation

### Create Commit with Multiple File Operations

**Pattern:** `POST /{repo_type}s/{namespace}/{name}/commit/{revision}`

**Authentication:** Required (write permission)

**Content-Type:** `application/x-ndjson` or `application/json`

**Purpose:** Atomic commit with multiple file operations (add/modify/delete/copy)

**Request Format:**

NDJSON (Newline-Delimited JSON) - one JSON object per line:

```ndjson
{"key": "header", "value": {"summary": "Update model", "description": "Improved accuracy"}}
{"key": "file", "value": {"path": "config.json", "content": "base64_content", "encoding": "base64"}}
{"key": "lfsFile", "value": {"path": "model.safetensors", "oid": "sha256_hash", "size": 5368709120, "algo": "sha256"}}
{"key": "deletedFile", "value": {"path": "old_file.txt"}}
{"key": "deletedFolder", "value": {"path": "old_folder/"}}
{"key": "copyFile", "value": {"path": "new_location.txt", "srcPath": "source.txt", "srcRevision": "main"}}
```

---

### Operation Types

#### 1. Header (Required)

**First line must be header:**
```json
{
  "key": "header",
  "value": {
    "summary": "Commit message",
    "description": "Optional detailed description",
    "parentCommit": "Optional commit id the branch must still point at"
  }
}
```

**`parentCommit`** (what `create_commit(parent_commit=...)` sends) is the commit id the client
expects the branch to point at, or its first characters. If the branch has moved on, nothing is
committed and the answer is `412 Precondition Failed`:

```json
{"error": "A commit has happened since. Please refresh and try again."}
```

**One commit at a time per branch.** A branch takes one commit request at a time, from the
moment its files are staged until everything is recorded. As with the Hub's concurrency queue, a
commit that arrives while another commit to the same branch is running waits for its turn (up to
20 seconds) and then goes through: concurrent commits all succeed, each in its own commit. A commit
that waits longer is refused the way the Hub refuses one, whoever it is: `429 Too Many Requests`
with `Retry-After: 2`, nothing committed and nothing left staged.

```json
{"error": "maximum time in concurrency queue reached"}
```

`huggingface_hub` retries a 429 with backoff (`http_backoff`, honouring `Retry-After`) where it
sends commits that way, and shows the message in the exception otherwise. The same message is in the
`X-Error-Message` header, which is where `huggingface_hub` reads it from. Commits to different
branches, and to different repositories, do not wait for each other. Name the `parentCommit` you
expect when a branch that has moved on must not be committed to: it is refused with `412` (above).
A commit that loses its place while it runs (its lease expired and was taken) is refused with `503`
and `Retry-After: 2`; nothing was committed, so it can be sent again.

A request whose `parentCommit` names a branch that does not exist is answered `404` with
`X-Error-Code: RevisionNotFound`.

---

#### 2. Regular File (Inline Base64)

**For files < LFS threshold:**
```json
{
  "key": "file",
  "value": {
    "path": "config.json",
    "content": "eyJtb2RlbCI6ICJiZXJ0In0=",
    "encoding": "base64"
  }
}
```

**Rules:**
- File size MUST be < LFS threshold
- Content is base64 encoded
- Encoding must be "base64"

**Error if file too large:**
```json
{
  "error": "File config.json should use LFS (size: 10000000 bytes, threshold: 5000000 bytes). Use 'lfsFile' operation instead.",
  "file_size": 10000000,
  "lfs_threshold": 5000000,
  "suggested_operation": "lfsFile"
}
```

---

#### 3. LFS File (Already Uploaded to S3)

**For files uploaded via LFS batch API:**
```json
{
  "key": "lfsFile",
  "value": {
    "path": "model.safetensors",
    "oid": "abc123def456789...",
    "size": 5368709120,
    "algo": "sha256"
  }
}
```

**Prerequisites:**
1. File must be uploaded to S3 via LFS batch API first
2. OID (SHA256) must match uploaded file
3. Size must match actual file size

**Server validates:**
- File exists in S3 at `lfs/{oid[:2]}/{oid[2:4]}/{oid}`
- Size matches S3 object size

---

#### 4. Delete File

**Remove a single file:**
```json
{
  "key": "deletedFile",
  "value": {
    "path": "old_model.bin"
  }
}
```

**Behavior:**
- Marks file as deleted in database (soft delete)
- Removes file from LakeFS branch
- Preserves LFS history for quota tracking

---

#### 5. Delete Folder

**Remove all files in a folder recursively:**
```json
{
  "key": "deletedFolder",
  "value": {
    "path": "old_experiments/"
  }
}
```

**Behavior:**
- Lists all files under folder recursively
- Deletes each file in parallel
- Marks files as deleted in database

---

#### 6. Copy File

**Copy file from same or different revision:**
```json
{
  "key": "copyFile",
  "value": {
    "path": "backup/model.safetensors",
    "srcPath": "model.safetensors",
    "srcRevision": "main"
  }
}
```

**Fields:**
- `path`: Destination path
- `srcPath`: Source file path
- `srcRevision`: Source revision (branch or commit, defaults to current revision)

**Behavior:**
- Links physical S3 address (no duplication)
- Copies database metadata
- Works for both regular and LFS files

---

### Response

**Success:**
```json
{
  "commitUrl": "http://localhost:28080/username/my-model/commit/abc123def",
  "commitOid": "abc123def456789...",
  "pullRequestUrl": null
}
```

**No Changes:**
```json
{
  "commitUrl": "http://localhost:28080/username/my-model/commit/previous_commit",
  "commitOid": "previous_commit_hash",
  "pullRequestUrl": null
}
```

---

## Complete Upload Workflows

### Example 1: Upload Large Model with Config (Single-Part LFS)

```python
import requests
import base64
import hashlib
import json

API_BASE = "http://localhost:28080/api"
REPO_ID = "username/my-model"
TOKEN = "your_token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Step 1: Preupload check
files_info = [
    {"path": "config.json", "size": 512},
    {"path": "model.safetensors", "size": 52428800, "sha256": "abc123..."}  # 50MB
]

preupload_resp = requests.post(
    f"{API_BASE}/models/{REPO_ID}/preupload/main",
    json={"files": files_info},
    headers=HEADERS
).json()

# Step 2: Upload files based on preupload response
lfs_files = []
regular_files = []

for file_info, preupload in zip(files_info, preupload_resp["files"]):
    if preupload["shouldIgnore"]:
        continue  # File already exists, skip

    if preupload["uploadMode"] == "lfs":
        # Upload via LFS batch API
        with open(file_info["path"], "rb") as f:
            content = f.read()
            sha256 = hashlib.sha256(content).hexdigest()

        # LFS batch request
        batch_resp = requests.post(
            f"{API_BASE}/{REPO_ID}.git/info/lfs/objects/batch",
            json={
                "operation": "upload",
                "transfers": ["basic"],
                "objects": [{"oid": sha256, "size": file_info["size"]}]
            },
            headers=HEADERS
        ).json()

        obj = batch_resp["objects"][0]
        if "actions" not in obj:
            # File already exists in LFS
            lfs_files.append({"path": file_info["path"], "oid": sha256, "size": file_info["size"]})
            continue

        # Single-part upload to S3
        with open(file_info["path"], "rb") as f:
            requests.put(obj["actions"]["upload"]["href"], data=f)

        # Verify
        requests.post(
            obj["actions"]["verify"]["href"],
            json={"oid": sha256, "size": file_info["size"]}
        )

        lfs_files.append({"path": file_info["path"], "oid": sha256, "size": file_info["size"]})

    else:
        # Regular file (base64)
        with open(file_info["path"], "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode()

        regular_files.append({
            "path": file_info["path"],
            "content": content_b64,
            "encoding": "base64"
        })

# Step 3: Create commit with all operations
ndjson_lines = [
    json.dumps({"key": "header", "value": {"summary": "Upload model", "description": "Initial upload"}})
]

for f in regular_files:
    ndjson_lines.append(json.dumps({"key": "file", "value": f}))

for f in lfs_files:
    ndjson_lines.append(json.dumps({"key": "lfsFile", "value": {
        "path": f["path"],
        "oid": f["oid"],
        "size": f["size"],
        "algo": "sha256"
    }}))

ndjson_payload = "\n".join(ndjson_lines)

commit_resp = requests.post(
    f"{API_BASE}/models/{REPO_ID}/commit/main",
    data=ndjson_payload,
    headers={**HEADERS, "Content-Type": "application/x-ndjson"}
).json()

print(f"Committed: {commit_resp['commitUrl']}")
```

---

### Example 2: Upload Very Large Model (Multipart LFS)

**For files ≥ 100MB (default multipart threshold):**

```python
import requests
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "http://localhost:28080/api"
REPO_ID = "username/my-model"
TOKEN = "your_token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def upload_large_file_multipart(file_path, repo_path):
    """Upload large file using LFS multipart protocol"""

    # Calculate SHA256
    print(f"Calculating SHA256 for {file_path}...")
    sha256_hash = hashlib.sha256()
    file_size = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha256_hash.update(chunk)
            file_size += len(chunk)

    sha256 = sha256_hash.hexdigest()
    print(f"SHA256: {sha256}, Size: {file_size:,} bytes")

    # Step 1: LFS batch request
    print("Requesting LFS batch upload URLs...")
    batch_resp = requests.post(
        f"{API_BASE}/{REPO_ID}.git/info/lfs/objects/batch",
        json={
            "operation": "upload",
            "transfers": ["basic"],
            "objects": [{"oid": sha256, "size": file_size}],
            "hash_algo": "sha256"
        },
        headers=HEADERS
    ).json()

    obj = batch_resp["objects"][0]

    # Check if file already exists
    if "actions" not in obj:
        print("File already exists in LFS storage (deduplication)")
        return {"oid": sha256, "size": file_size, "path": repo_path}

    upload_action = obj["actions"]["upload"]
    verify_action = obj["actions"]["verify"]

    # Check if multipart
    if "header" in upload_action and "chunk_size" in upload_action["header"]:
        # Multipart upload
        header = upload_action["header"]
        chunk_size = int(header["chunk_size"])
        upload_id = header["upload_id"]

        print(f"Multipart upload: chunk_size={chunk_size:,} bytes")

        # Calculate number of parts
        num_parts = math.ceil(file_size / chunk_size)
        print(f"Uploading {num_parts} parts in parallel...")

        # Upload parts in parallel
        parts = []

        def upload_part(part_number):
            """Upload a single part"""
            part_url = header[str(part_number)]

            # Read chunk
            with open(file_path, "rb") as f:
                f.seek((part_number - 1) * chunk_size)
                chunk = f.read(chunk_size)

            # Upload
            resp = requests.put(part_url, data=chunk)
            resp.raise_for_status()

            # Extract ETag (remove quotes if present)
            etag = resp.headers["ETag"].strip('"')

            print(f"  Part {part_number}/{num_parts} uploaded (ETag: {etag[:8]}...)")
            return {"PartNumber": part_number, "ETag": etag}

        # Upload parts concurrently (max 10 parallel)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(upload_part, i) for i in range(1, num_parts + 1)]

            for future in as_completed(futures):
                parts.append(future.result())

        # Sort parts by part number
        parts.sort(key=lambda p: p["PartNumber"])

        # Step 2: Complete multipart upload
        print("Completing multipart upload...")
        complete_resp = requests.post(
            f"{API_BASE}/{REPO_ID}.git/info/lfs/complete/{upload_id}",
            json={
                "oid": sha256,
                "size": file_size,
                "upload_id": upload_id,
                "parts": parts
            }
        )
        complete_resp.raise_for_status()
        print("Multipart upload completed")

        # Step 3: Verify with multipart info
        print("Verifying upload...")
        verify_resp = requests.post(
            verify_action["href"],
            json={
                "oid": sha256,
                "size": file_size,
                "upload_id": upload_id,
                "parts": parts
            }
        )
        verify_resp.raise_for_status()
        print("Upload verified")

    else:
        # Single-part upload (< 100MB)
        print("Single-part upload...")
        with open(file_path, "rb") as f:
            requests.put(upload_action["href"], data=f)

        # Verify
        requests.post(
            verify_action["href"],
            json={"oid": sha256, "size": file_size}
        )
        print("Upload complete")

    return {"oid": sha256, "size": file_size, "path": repo_path}

# Upload large model file
model_info = upload_large_file_multipart(
    "large_model.safetensors",  # Local file (e.g., 5GB)
    "model.safetensors"         # Path in repo
)

# Upload config (regular file)
with open("config.json", "rb") as f:
    config_b64 = json.dumps(json.load(f)).encode()
    config_b64_str = base64.b64encode(config_b64).decode()

# Create commit
ndjson_lines = [
    json.dumps({"key": "header", "value": {"summary": "Upload large model", "description": "5GB model with config"}}),
    json.dumps({"key": "lfsFile", "value": {
        "path": model_info["path"],
        "oid": model_info["oid"],
        "size": model_info["size"],
        "algo": "sha256"
    }}),
    json.dumps({"key": "file", "value": {
        "path": "config.json",
        "content": config_b64_str,
        "encoding": "base64"
    }})
]

commit_resp = requests.post(
    f"{API_BASE}/models/{REPO_ID}/commit/main",
    data="\n".join(ndjson_lines),
    headers={**HEADERS, "Content-Type": "application/x-ndjson"}
).json()

print(f"Committed: {commit_resp['commitUrl']}")
```

**Key Points:**
- **Chunk size:** 50MB default (configurable)
- **Parallel uploads:** Up to 10 parts concurrently
- **Progress tracking:** Each part reports completion
- **Resume support:** Can retry failed parts without restarting
- **ETags:** Required for multipart completion

---

### Multipart Upload Details

**Thresholds (configurable via environment variables):**

```bash
# When to use multipart (default: 100MB)
KOHAKU_HUB_LFS_MULTIPART_THRESHOLD_BYTES=104857600

# Size of each part (default: 50MB, min: 5MB)
KOHAKU_HUB_LFS_MULTIPART_CHUNK_SIZE_BYTES=52428800
```

**Multipart Flow:**

1. **Batch Request** → Server returns part URLs in `header` object
2. **Upload Parts** → PUT each part in parallel, collect ETags
3. **Complete** → POST to `/lfs/complete/{upload_id}` with ETags
4. **Verify** → POST to `/lfs/verify` with upload_id and parts

**Part URL Format:**
```json
{
  "header": {
    "chunk_size": "52428800",
    "upload_id": "s3_upload_id_xxx",
    "1": "https://s3.../uploadId=xxx&partNumber=1&...",
    "2": "https://s3.../uploadId=xxx&partNumber=2&...",
    "10": "https://s3.../uploadId=xxx&partNumber=10&..."
  }
}
```

**ETag Collection:**
```python
# From each part upload response
etag = response.headers["ETag"].strip('"')
parts.append({"PartNumber": part_num, "ETag": etag})
```

**Complete Request:**
```json
{
  "oid": "sha256_hash",
  "size": 524288000,
  "upload_id": "s3_upload_id",
  "parts": [
    {"PartNumber": 1, "ETag": "etag1"},
    {"PartNumber": 2, "ETag": "etag2"}
  ]
}
```

---

## Browser File Upload

### Upload from Web Browser

**Pattern:** Same as above, but set `is_browser: true` in LFS batch request

**Why?**
- Browser uploads need `Content-Type` header in presigned URL
- Server includes it automatically when `is_browser: true`

**Example:**
```javascript
// LFS batch request from browser
const batchResp = await fetch('/api/models/user/repo.git/info/lfs/objects/batch', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    operation: 'upload',
    transfers: ['basic'],
    objects: [{oid: sha256, size: fileSize}],
    is_browser: true  // Important!
  })
});

const {objects} = await batchResp.json();
const uploadUrl = objects[0].actions.upload.href;

// Upload file (browser automatically adds Content-Type)
await fetch(uploadUrl, {
  method: 'PUT',
  body: file
});
```

---

## Deduplication

### Content-Based Deduplication

**How it works:**
1. Client provides SHA256 hash in preupload
2. Server checks if file with same hash already exists
3. If exists: `shouldIgnore: true` (skip upload)
4. If not exists: `shouldIgnore: false` (upload required)

**Benefits:**
- Saves bandwidth (no redundant uploads)
- Saves storage (same content = same S3 object)
- Faster uploads (skip unchanged files)

**Example:**
```json
{
  "files": [
    {
      "path": "config.json",
      "size": 512,
      "sha256": "abc123...",
      "sample": "eyJtb2RlbCI6..."
    }
  ]
}
```

**Response if duplicate:**
```json
{
  "files": [
    {
      "path": "config.json",
      "uploadMode": "regular",
      "shouldIgnore": true
    }
  ]
}
```

---

## Quota Management

### Quota Checks

**During preupload:**
- Total upload size calculated from all files
- Quota checked against namespace (user or org)
- Based on repository privacy (public vs private)

**Error if quota exceeded:**
```json
{
  "error": "Storage quota exceeded",
  "message": "You have used 9.5 GB of your 10 GB quota. This upload requires 2.5 GB."
}
```

**Status Code:** `413 Payload Too Large`

---

## Error Handling

### Common Errors

**400 Bad Request - File too large for inline:**
```json
{
  "error": "File should use LFS (size: 10000000 bytes, threshold: 5000000 bytes)",
  "suggested_operation": "lfsFile"
}
```

**400 Bad Request - LFS object not found:**
```json
{
  "error": "LFS object abc123... not found in storage. Upload to S3 may have failed."
}
```

**404 Not Found - Repository:**
```json
{
  "error": "Repository not found"
}
```

**403 Forbidden - Permission:**
```json
{
  "error": "You don't have write access to this repository"
}
```

**413 Payload Too Large - Quota:**
```json
{
  "error": "Storage quota exceeded",
  "message": "..."
}
```

---

## Performance Tips

**For small repos (<100 files):**
- Use regular files when possible (< 5MB)
- Batch operations in single commit
- Enable deduplication with SHA256

**For large repos (100+ files):**
- Always use LFS for files >5MB
- Upload LFS files in parallel
- Use multipart for files >100MB
- Commit frequently (don't batch 1000+ files)

**For CI/CD:**
- Cache LFS objects locally
- Skip unchanged files with deduplication
- Use shallow clones
- Upload only changed files

---

## Next Steps

- [Git LFS API](./git-lfs.md) - LFS batch protocol details
- [Branches API](./branches.md) - Branch/tag management
- [Commits API](./commits.md) - Commit history
