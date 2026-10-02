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
      "revert": true,
      "reset": true,
      "squash": true
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

### Defaults

Revert, Reset, and Super Squash are **enabled by default**. They were
disabled while #99 and #107 were fixed (silent data corruption, long hangs and
outages in real deployments). Each now changes LakeFS metadata only and takes
about a second, whatever the repository's size.

Each flag stays as an off switch. Set `KOHAKU_HUB_REPOSITORY_REVERT_ENABLED`,
`KOHAKU_HUB_REPOSITORY_RESET_ENABLED`, or
`KOHAKU_HUB_REPOSITORY_SQUASH_ENABLED` to `false`, or the matching
`repository_*_enabled` key under `[app]` in `config.toml`.

An operation is available only when:

- `db_backend = "postgres"`. Any other backend keeps all three disabled.
- For Reset, the LakeFS server is 1.48.1 or later, the oldest release it is
  verified on. On an older LakeFS it is disabled and its `503` says so. See
  [LakeFS compatibility](../deployment/lakefs.md).

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

**Recorded like a commit:** before answering, the File table, LFS history, branch head references, commit record (with user attribution) and storage usage are updated for every path the merge changed, read to the end.

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
- `parent_number`: The parent to revert against, for merge commits (default: 1, which undoes what the merge brought in)
- `message`: Commit message (optional; default `Revert commit <id>`)
- `metadata`: Additional metadata (optional)
- `force`: Accepted and ignored. LakeFS refuses a conflict or uncommitted changes either way.
- `allow_empty`: Make an empty commit when there is nothing to revert, instead of answering 400

**Response:**
```json
{
  "success": true,
  "message": "Successfully reverted commit abc123de on branch 'main'",
  "new_commit_id": "new_commit_hash"
}
```

**Rules**, per path the commit changed, against the branch head:
- the head still has the commit's version: it goes back to the parent's version (undone);
- the head already has the parent's version: left alone;
- anything else is a **conflict**, and nothing is reverted.

**Checked before reverting:**
- conflicts: answered 409, with the files in `conflicts`;
- the parent versions it restores: they must still be stored (not garbage collected, present in the bucket), or 400 with `missing_files`. There is no way around this.
- something to revert: otherwise 400 "Nothing to revert", unless `allow_empty`;
- the repository's first commit has no parent: 400.

**How it works:**
- LakeFS reverts natively: a three-way merge of metadata, committed atomically, with no file content through the API. It works on the branch head at that moment, so a concurrent commit needs no retry.
- The versions it restores are claimed against garbage collection while it runs.
- The new commit is found by a marker in its metadata (`kh_operation`), not by reading the head, which may already be someone else's commit.
- Before answering, the File table, LFS history, branch head references, commit record and storage usage are updated for the paths it changed. The versions it replaced are left to garbage collection, which runs in the background.
- An upload in flight on the branch is waited for; uncommitted changes that stay answer 409.

**Status Codes:**
- `200 OK` - Reverted successfully
- `400 Bad Request` - Nothing to revert, versions no longer stored, the first commit, or an invalid `parent_number`
- `404 Not Found` - Commit not found
- `404 Not Found` - Branch not found
- `409 Conflict` - Later commits changed the same files (listed), or the branch has uncommitted changes
- Other `4xx` - LakeFS refused the revert (a protected branch, a hook): its status and message
- `500 Internal Server Error` - The revert failed, or it may have been applied but its commit could not be found; the branch's references are then reconciled
- `503 Service Unavailable` - Revert operation is disabled by server policy

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
- `409 Conflict` - The branch has uncommitted changes (an upload in progress); try again
- `503 Service Unavailable` - Reset operation is disabled by server policy

**How it works:**
- A two-dot diff between the head and the target, read to the end, gives every path to change.
- The reset is one LakeFS commit of the target's own metarange (`source_metarange`) on top of the branch head. It is applied atomically, and no object is copied or linked, so it works whatever the object store (#133). Recording the changed regular files' git blob ids still reads them, as before. A failure before that commit leaves the branch as it was.
- A commit that lands concurrently becomes the reset commit's parent. The result still equals the target, the concurrent commit stays in the history, and the paths it changed are claimed and recorded too. If the branch came to equal the target meanwhile, the reset commit changes nothing more and is recorded like any other.
- The initial commit has no metarange (an empty tree): a scratch branch with every file deleted provides one.
- An error after the reset commit landed (a missing file, a failure) names it in `commits`; it is on the branch and recorded.
- Before answering, the File table, LFS history, branch head references, commit record and storage usage are updated for the paths that changed, from what the branch holds. The versions replaced are left to garbage collection, which runs in the background.

**Changed from earlier versions:** `force` no longer skips the LFS check, the 400 body has no `affected_commits`, a target equal to the head answers 400 (not 500), and 409 is new.

---

### Super Squash a Branch

**Pattern:** `POST /api/{repo_type}s/{namespace}/{name}/super-squash/{branch}`

Hugging Face's `super_squash_history`: the branch becomes a single commit with its current tree, in place, in about a second whatever the repository's size. Other branches and tags are kept, and so is the history they still reach. Squashing a whole repository (which also deletes the other branches and tags) is `POST /api/repos/squash`.

**Request Body (optional):**
```json
{"message": "Super-squash branch 'main'"}
```

**Response:** `{"commitOid": "<the squash commit>"}`

**Status Codes:** `200`; `404` for a missing repository or branch (a tag cannot be squashed); `409` while another operation holds the repository; `503` when Squash is disabled.

A squashed branch shares no history with the branches it came from, so merging one into the other answers `409`.

```python
from huggingface_hub import HfApi

HfApi(endpoint="https://hub.example.com").super_squash_history("my-org/my-model", branch="dev")
```

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
