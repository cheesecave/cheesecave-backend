---
title: Branches & Tags API
description: Branch/tag management, merge, revert, reset operations
icon: i-carbon-branch
---

# Branches & Tags API

Manage branches, tags, and advanced Git operations (merge, revert, reset).

---

## Repository Operation Gate

The history-mutating operations below are independently controlled by server-side
capabilities: `revert`, `reset`, and `squash`. The public capability state is
available without authentication from `GET /api/site-config`:

```json
{
  "capabilities": {
    "repository_operations": {
      "revert": false,
      "reset": false,
      "squash": false
    }
  }
}
```

Clients must treat an operation as enabled only when its value is the boolean
`true`. If the request fails, the field is missing, or the value has any other
type, clients must fail closed and hide the corresponding action.

When an operation is disabled, its API gate runs before authentication and
repository lookup. The server returns `503 Service Unavailable` with this
stable response shape:

```json
{
  "detail": {
    "code": "operation_disabled",
    "operation": "reset",
    "error": "Repository reset is temporarily disabled",
    "message": "Repository Reset is temporarily disabled"
  }
}
```

The `operation` value is one of `revert`, `reset`, or `squash`. Once enabled,
the normal authentication, permission, and repository validation requirements
still apply.

### Defaults and reopening plan

Revert, Reset, and Super Squash are **disabled by default for now**. Real
deployments showed silent data corruption, long hangs, and outages in them
(#99), and #107 records further integrity gaps. The flags exist so these
operations can be reopened safely, one at a time. The plan:

1. Each operation gets its integrity and recovery fixes from the #99 work
   plan, plus a canary run in a real deployment.
2. Operations then become available one by one. An operator can opt in to an
   individual operation with its flag before the defaults change.
3. Once all three have passed, the defaults switch to **enabled**. The flags
   stay as emergency off switches.

To opt in before then, set `KOHAKU_HUB_REPOSITORY_REVERT_ENABLED`,
`KOHAKU_HUB_REPOSITORY_RESET_ENABLED`, or
`KOHAKU_HUB_REPOSITORY_SQUASH_ENABLED` to `true`. Alternatively, set
`repository_revert_enabled`, `repository_reset_enabled`, or
`repository_squash_enabled` under `[app]` in `config.toml`. Opting in means
accepting the open issues in #99 and #107, and requires:

- `db_backend = "postgres"`. Any other backend keeps every operation
  disabled.
- Draining backend instances that predate the gates, and deploying the
  frontend with this change, before enabling a flag.
- A backup of the repositories and object storage these operations may
  touch.

## Branches

### Create Branch

**Pattern:** `POST /api/{repo_type}s/{namespace}/{name}/branch`

**Authentication:** Required (delete permission)

**Request Body:**
```json
{
  "branch": "feature-branch",
  "revision": "main"
}
```

**Fields:**
- `branch`: New branch name (required)
- `revision`: Source revision (branch/commit, default: "main")

**Response:**
```json
{
  "success": true,
  "message": "Branch 'feature-branch' created"
}
```

**Status Codes:**
- `200 OK` - Branch created
- `404 Not Found` - Repository or source revision not found
- `409 Conflict` - Branch already exists

---

### Delete Branch

**Pattern:** `DELETE /api/{repo_type}s/{namespace}/{name}/branch/{branch}`

**Authentication:** Required (delete permission)

**Protection:** Cannot delete "main" branch

**Response:**
```json
{
  "success": true,
  "message": "Branch 'feature-branch' deleted"
}
```

**Status Codes:**
- `200 OK` - Branch deleted
- `400 Bad Request` - Attempting to delete main branch
- `404 Not Found` - Repository or branch not found

---

### Merge Branches

**Pattern:** `POST /api/{repo_type}s/{namespace}/{name}/merge/{source_ref}/into/{destination_branch}`

**Authentication:** Required (write permission)

**Request Body:**
```json
{
  "message": "Merge feature into main",
  "metadata": {
    "author": "alice"
  },
  "strategy": "dest-wins",
  "force": false,
  "allow_empty": false,
  "squash_merge": false
}
```

**Fields (all optional):**
- `message`: Commit message
- `metadata`: Additional metadata
- `strategy`: Conflict resolution
  - `"dest-wins"`: Destination wins on conflict
  - `"source-wins"`: Source wins on conflict
  - `null`: Fail on conflict (default)
- `force`: Force merge even with conflicts
- `allow_empty`: Allow merge with no changes
- `squash_merge`: Squash all commits into one

**Response:**
```json
{
  "success": true,
  "message": "Successfully merged feature into main",
  "result": {
    "reference": "commit_hash",
    "summary": {
      "added": 5,
      "removed": 2,
      "changed": 3
    }
  }
}
```

**Status Codes:**
- `200 OK` - Merged successfully
- `409 Conflict` - Merge conflict (use strategy to resolve)
- `500 Internal Server Error` - Merge failed

**Auto-tracking:**
- LFS objects tracked automatically
- Commit recorded in database
- User attribution preserved

---

### Revert Commit

**Pattern:** `POST /api/{repo_type}s/{namespace}/{name}/branch/{branch}/revert`

**Authentication:** Required (write permission)

**Purpose:** Create new commit that undoes changes from a specific commit

**Request Body:**
```json
{
  "ref": "commit_id_to_revert",
  "parent_number": 1,
  "message": "Revert: broken feature",
  "metadata": {
    "reason": "broke production"
  },
  "force": false,
  "allow_empty": false
}
```

**Fields:**
- `ref`: Commit ID or ref to revert (required)
- `parent_number`: For merge commits (default: 1)
- `message`: Commit message (optional)
- `metadata`: Additional metadata (optional)
- `force`: Force revert even with conflicts
- `allow_empty`: Allow empty revert

**Response:**
```json
{
  "success": true,
  "message": "Successfully reverted commit abc123de on branch 'main'",
  "new_commit_id": "new_commit_hash"
}
```

**Important Notes:**
- Creates NEW commit (doesn't delete history)
- LFS files automatically handled (no recoverability check needed)
- If conflicts: returns 409 error

**Status Codes:**
- `200 OK` - Reverted successfully
- `503 Service Unavailable` - Revert operation is disabled by server policy
- `404 Not Found` - Commit not found
- `409 Conflict` - Revert caused conflicts

---

### Reset Branch

**Pattern:** `POST /api/{repo_type}s/{namespace}/{name}/branch/{branch}/reset`

**Authentication:** Required (write permission)

**Purpose:** Make the branch's content equal a commit's, with a new commit on top (history is kept, so a reset can itself be reset)

**Request Body:**
```json
{
  "ref": "commit_id_to_reset_to",
  "message": "Reset to stable version",
  "force": false
}
```

**Fields:**
- `ref`: Commit ID or ref to reset to (required)
- `message`: Commit message (optional; default `Reset to commit <id>`)
- `force`: Required to reset `main`. It no longer skips the LFS check.

**Response:**
```json
{
  "success": true,
  "message": "Successfully reset branch 'main' to commit abc123de (new commit created)",
  "commit_id": "new_commit_hash"
}
```

**LFS check:** every LFS object the target needs and the branch head lacks
must still be stored: not garbage collected (tombstoned) and present in the
bucket. Objects the head already has need nothing restored. Otherwise:

```json
{
  "error": "Cannot reset to commit abc123de: 2 LFS file(s) are no longer stored (garbage collected or missing): model.safetensors, data.bin",
  "missing_files": ["model.safetensors", "data.bin"],
  "recoverable": false
}
```

**Status Codes:**
- `200 OK` - Reset successful
- `400 Bad Request` - LFS files no longer stored, `main` without `force`, or the branch is already at the target state
- `404 Not Found` - Commit not found
- `409 Conflict` - The branch kept changing during the reset (concurrent commits); try again
- `503 Service Unavailable` - Reset operation is disabled by server policy

**How it works:**
- A two-dot diff between the head and the target, read to the end, gives every path to change.
- No file content passes through the API: LFS files link their global `lfs/<sha256>` object again (same identity), regular files are copied inside the object store, removed files are deleted in batches. `lakefs.operation_concurrency` bounds the concurrent LakeFS requests.
- The new tree is built on a scratch branch and squash-merged onto the branch in one step, so readers never see a half-done reset, and a failure leaves the branch as it was.
- The result must equal the target: when a concurrent commit got in, the reset runs again from the new head (at most three merges). The concurrent commits stay in the history.
- Before answering, the File table, LFS history, branch head references and commit record are updated for the paths that changed. The versions replaced are left to garbage collection, which runs in the background.

---

## Tags

### Create Tag

**Pattern:** `POST /api/{repo_type}s/{namespace}/{name}/tag`

**Authentication:** Required (delete permission)

**Request Body:**
```json
{
  "tag": "v1.0.0",
  "revision": "main",
  "message": "Release version 1.0"
}
```

**Fields:**
- `tag`: Tag name (required)
- `revision`: Source revision (default: "main")
- `message`: Tag message (optional)

**Response:**
```json
{
  "success": true,
  "message": "Tag 'v1.0.0' created"
}
```

**Status Codes:**
- `200 OK` - Tag created
- `404 Not Found` - Repository or revision not found
- `500 Internal Server Error` - Failed to create tag

---

### Delete Tag

**Pattern:** `DELETE /api/{repo_type}s/{namespace}/{name}/tag/{tag}`

**Authentication:** Required (delete permission)

**Response:**
```json
{
  "success": true,
  "message": "Tag 'v1.0.0' deleted"
}
```

**Status Codes:**
- `200 OK` - Tag deleted
- `404 Not Found` - Repository or tag not found
- `500 Internal Server Error` - Failed to delete tag

---

## Usage Examples

### Merge Feature Branch

```python
import requests

API_BASE = "http://localhost:28080/api"
TOKEN = "your_token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Merge feature into main
response = requests.post(
    f"{API_BASE}/models/username/repo/merge/feature-branch/into/main",
    json={
        "message": "Merge feature: Add new model",
        "strategy": "dest-wins"  # Main wins on conflicts
    },
    headers=HEADERS
)

result = response.json()
print(f"Merged: {result['message']}")
print(f"Commit: {result['result']['reference']}")
```

---

### Revert Bad Commit

```python
# Find the bad commit
commits_resp = requests.get(
    f"{API_BASE}/models/username/repo/commits/main?limit=10",
    headers=HEADERS
)
commits = commits_resp.json()["commits"]
bad_commit = commits[0]["id"]  # Latest commit

# Revert it
revert_resp = requests.post(
    f"{API_BASE}/models/username/repo/branch/main/revert",
    json={
        "ref": bad_commit,
        "message": "Revert: broken model weights"
    },
    headers=HEADERS
)

result = revert_resp.json()
print(f"Reverted: {result['new_commit_id']}")
```

---

### Reset to Previous Version

```python
# Reset to 3 commits ago
commits_resp = requests.get(
    f"{API_BASE}/models/username/repo/commits/main?limit=5",
    headers=HEADERS
)
commits = commits_resp.json()["commits"]
target_commit = commits[3]["id"]  # 3 commits back

# Reset (main needs force; LFS objects are always checked)
reset_resp = requests.post(
    f"{API_BASE}/models/username/repo/branch/main/reset",
    json={
        "ref": target_commit,
        "message": "Reset to stable version",
        "force": True,
    },
    headers=HEADERS
)

if reset_resp.status_code == 400:
    # Already at the target, or LFS files no longer stored
    error = reset_resp.json()["detail"]
    print(f"Cannot reset: {error['error']}")
    print(f"Missing files: {error.get('missing_files', [])}")
    raise SystemExit(1)

result = reset_resp.json()
print(f"Reset to: {result['commit_id']}")
```

---

### Create Release Tag

```python
# Tag current main branch
tag_resp = requests.post(
    f"{API_BASE}/models/username/repo/tag",
    json={
        "tag": "v1.0.0",
        "revision": "main",
        "message": "Release version 1.0 - Production ready"
    },
    headers=HEADERS
)

result = tag_resp.json()
print(result["message"])

# Download specific version
file_url = f"{API_BASE}/models/username/repo/resolve/v1.0.0/model.safetensors"
```

---

## Git Command Equivalents

### Branches

```bash
# Create branch
POST /branch {"branch": "feature", "revision": "main"}
# git checkout -b feature main

# Delete branch
DELETE /branch/feature
# git branch -d feature

# Merge
POST /merge/feature/into/main {"strategy": "dest-wins"}
# git checkout main && git merge feature -X theirs
```

### Tags

```bash
# Create tag
POST /tag {"tag": "v1.0", "revision": "main"}
# git tag v1.0 main

# Delete tag
DELETE /tag/v1.0
# git tag -d v1.0
```

### Advanced Operations

```bash
# Revert
POST /branch/main/revert {"ref": "abc123"}
# git revert abc123

# Reset
POST /branch/main/reset {"ref": "abc123"}
# git reset --hard abc123 (but creates new commit)
```

---

## Best Practices

**Branching Strategy:**
- Use `main` for production
- Create feature branches for development
- Merge with `dest-wins` to protect main
- Tag releases for versioning

**LFS Considerations:**
- Reset checks LFS availability (prevent broken refs)
- Use `force: true` only if you understand the risk
- Keep `lfs_keep_versions` high enough for your workflow
- Monitor storage after resets/reverts

**Safety:**
- Always use `force: false` first (see errors)
- Test merges on feature branches first
- Tag before risky operations (easy rollback)
- Document reset/revert reasons in commit message

---

## Next Steps

- [Commits API](./commits.md) - View commit history
- [File Upload API](./file-upload.md) - Commit files
- [Repositories API](./repositories.md) - Repo management
