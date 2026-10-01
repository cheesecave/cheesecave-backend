---
title: Repository API
description: CRUD operations for models, datasets, and spaces
icon: i-carbon-data-base
---

# Repository API

Create, read, update, delete, move, and squash repositories.

---

## Create Repository

**Pattern:** `POST /api/repos/create`

**Authentication:** Required

**Request Body:**
```json
{
  "type": "model",
  "name": "my-model",
  "organization": null,
  "private": false,
  "sdk": null
}
```

**Fields:**
- `type`: Repository type (`"model"`, `"dataset"`, or `"space"`)
- `name`: Repository name (required)
- `organization`: Organization name (optional, defaults to user namespace)
- `private`: Privacy setting (default: false)
- `sdk`: SDK hint (optional, reserved for future use)

**Response:**
```json
{
  "url": "http://localhost:28080/models/username/my-model",
  "repo_id": "username/my-model"
}
```

**Status Codes:**
- `200 OK` - Repository created
- `400 Bad Request` - Repository already exists or name conflicts

**Validation:**
- Name conflicts checked (case-insensitive)
- User must have permission to use namespace
- Normalized name conflicts prevented (e.g., "My-Model" vs "my_model")

---

## Delete Repository

**Pattern:** `DELETE /api/repos/delete`

**Authentication:** Required (user or admin token)

**Request Body:**
```json
{
  "type": "model",
  "name": "my-model",
  "organization": null,
  "sdk": null
}
```

**Fields:**
- `type`: Repository type
- `name`: Repository name
- `organization`: Organization name (optional for users, required for admin)

**Admin Usage:**
- Accepts `X-Admin-Token` header
- Admin must specify `organization` parameter

**Response:**
```json
{
  "message": "Repository 'username/my-model' of type 'model' deleted."
}
```

**Cleanup Process:**
1. Deletes S3 storage (repo objects + unused LFS objects)
2. Deletes LakeFS repository
3. Deletes database records (CASCADE: files, commits, LFS history)

**Status Codes:**
- `200 OK` - Repository deleted
- `403 Forbidden` - No delete permission
- `404 Not Found` - Repository not found

**WARNING:** This operation is **IRREVERSIBLE**

---

## Move/Rename Repository

A move or rename keeps everything: the commit history, all branches, and all
tags. Only the repository's name changes; no data is copied. The old name is
free again immediately, and a new repository created under it is independent
of the moved one.

**Pattern:** `POST /api/repos/move`

**Authentication:** Required (user or admin token)

**Request Body:**
```json
{
  "fromRepo": "alice/old-name",
  "toRepo": "alice/new-name",
  "type": "model"
}
```

**Use Cases:**
1. **Rename:** `alice/model-v1` → `alice/model-v2`
2. **Move to org:** `alice/model` → `my-org/model`
3. **Transfer:** `alice/model` → `bob/model`

**Process:**
1. Validates source repository exists
2. Checks permissions (source delete + target namespace); the target namespace must be an existing user or organization
3. Validates quota for namespace changes (skipped for an admin token)
4. Renames the repository in one database transaction: its LakeFS repository, and so every commit, branch, tag and file, stays as it is

**Ownership:** a repository belongs to the user or organization its namespace names. Moving it to another namespace hands it over, with its files and commits: deleting the account it came from no longer deletes it. Its storage usage moves from one namespace to the other, also for an admin's move.

**Response:**
```json
{
  "success": true,
  "url": "http://localhost:28080/models/bob/model",
  "message": "Repository moved from alice/model to bob/model"
}
```

**Quota Check:**
- Only for namespace changes (user → org, or user → user)
- Based on repository privacy (public/private)
- Includes full repository size

**Status Codes:**
- `200 OK` - Repository moved
- `400 Bad Request` - Invalid IDs, destination exists, or quota exceeded
- `403 Forbidden` - No permission
- `404 Not Found` - Source repository or target namespace not found

---

## Squash Repository

**Pattern:** `POST /api/repos/squash`

**Authentication:** Required (user or admin token)

**Purpose:** Clear all commit history, keep only current state

**Request Body:**
```json
{
  "repo": "username/my-model",
  "type": "model",
  "message": "Squash history"
}
```

`message` is optional (default `Squash history`).

**What it does:**
1. `main` becomes a single commit with its current tree and no parent. It is made in place with LakeFS metadata operations, so nothing is copied and the repository keeps its name and data throughout. It takes about a second whatever the repository's size.
2. The other branches and tags are deleted: only the current state is kept. The commits the squash commit does not reach are out of the history: every endpoint answers `404 RevisionNotFound` for them, and nothing restores them.
3. A few minutes later, in the background (`storage.forget_squashed_history`):
   - the history and file rows of what is no longer reachable are forgotten;
   - the regular file objects only the old history had are deleted from storage;
   - LFS garbage collection removes the objects no repository relies on any more.

   The repository's usage drops with them.

While it runs, other writes to the repository wait for it; the ones that have not started answer `409` with `Retry-After`. A commit that was already uploading lands on top of the squash commit.

To squash one branch and keep the others, use Hugging Face's `super_squash_history` (`POST /api/{repo_type}s/{namespace}/{name}/super-squash/{branch}`, see [Branch API](branches.md)).

**Response:**
```json
{
  "success": true,
  "message": "Repository username/my-model squashed successfully. All commit history has been cleared."
}
```

**Important Notes:**
- **IRREVERSIBLE** - All history deleted
- Old LFS versions are collected in the background; objects other repositories link are kept. Until the collection removes one, it stays downloadable by its LFS oid (LFS objects are content-addressed and shared across repositories)
- Repository quota preserved

**Status Codes:**
- `200 OK` - Repository squashed
- `503 Service Unavailable` - Squash operation is disabled by server policy
- `403 Forbidden` - No permission
- `404 Not Found` - Repository not found
- `409 Conflict` - Another operation holds the repository, or `main` kept changing; retry
- `502 Bad Gateway` - `main` was squashed but deleting another branch or tag failed; squash again to finish

**Operation gate:** The server exposes the current `revert`, `reset`, and
`squash` capabilities without authentication through `GET /api/site-config`.
Clients should use an operation only when its capability is exactly the boolean
`true`; missing, malformed, or failed capability responses must hide the
corresponding action. When squash is disabled, the API returns `503` before
authentication or repository lookup with a stable `operation_disabled` detail:

```json
{
  "detail": {
    "code": "operation_disabled",
    "operation": "squash",
    "error": "Repository squash is temporarily disabled",
    "message": "Repository Squash is temporarily disabled"
  }
}
```

When enabled, the normal authentication and permission checks still apply.

Squash is enabled by default and can be switched off; see
[Defaults](branches.md#defaults).

---

## Get Repository Info

**Pattern:** `GET /api/{repo_type}s/{namespace}/{name}`

**Examples:**
- `GET /api/models/username/bert-base`
- `GET /api/datasets/org/imagenet`
- `GET /api/spaces/user/gradio-app`

**Authentication:** Optional (required for private repos)

**Query parameters**, as on the Hugging Face Hub:

| Parameter | Effect |
| --- | --- |
| *(none)* | Every field, and `siblings`: the name (`rfilename`) of every file. This is what `HfApi.model_info()` / `dataset_info()` / `repo_info()` ask by default, and what `snapshot_download` lists. |
| `blobs=true` | `siblings` also carry `blobId` and `size`, and `lfs` for LFS files (`files_metadata=True`). |
| `expand=<property>` (repeatable) | Only `_id`, `id` and the named properties. `siblings` is listed only when named (or with `blobs=true`). An unknown property answers `400`, as on the Hub. |

`siblings` lists the whole repository, so it costs in proportion to the file count; the name-only list is kept per commit after the first request. A client after the repository's metadata alone should use `expand` (the web UI asks for `sha`, `lastModified`, `createdAt`, `private`, `downloads`, `likes`, `tags` and `storage`).

The accepted `expand` properties are the Hub's for each repository type (for models, for example, `sha`, `lastModified`, `private`, `downloads`, `likes`, `tags`, `siblings`, `usedStorage`, `cardData`, `safetensors`...), plus KohakuHub's `storage`. Properties KohakuHub keeps no value for are returned as `null`.

**Response** (no parameters):
```json
{
  "_id": 1,
  "id": "username/bert-base",
  "modelId": "username/bert-base",
  "author": "username",
  "sha": "commit_hash",
  "lastModified": "2025-01-20T10:15:32.123Z",
  "createdAt": "2025-01-15T08:00:00.000Z",
  "private": false,
  "disabled": false,
  "gated": false,
  "downloads": 150,
  "downloadsAllTime": 150,
  "likes": 25,
  "tags": [],
  "pipeline_tag": null,
  "library_name": null,
  "usedStorage": 5368710144,
  "spaces": [],
  "models": [],
  "datasets": [],
  "storage": {
    "quota_bytes": 10737418240,
    "used_bytes": 5368710144,
    "available_bytes": 5368708096,
    "percentage_used": 50.0,
    "effective_quota_bytes": 10737418240,
    "is_inheriting": false
  },
  "siblings": [
    {"rfilename": "config.json"},
    {"rfilename": "model.safetensors"}
  ]
}
```

With `blobs=true`, each sibling looks like:
```json
{"rfilename": "config.json", "blobId": "5b6e8f...", "size": 1024}
{"rfilename": "model.safetensors", "blobId": "90e59f...", "size": 5368709120,
 "lfs": {"sha256": "abc123...", "size": 5368709120, "pointerSize": 134}}
```

- `blobId`: the git blob id, of the file, or of its LFS pointer for an LFS file.
- `lfs`: present for the files stored as LFS objects; `sha256` is the object's.

With `expand=sha&expand=private`:
```json
{"_id": 1, "id": "username/bert-base", "sha": "commit_hash", "private": false}
```

**Fields:**
- `storage`: KohakuHub's; only for authenticated users
- `sha`: Latest commit on main branch

`GET /api/{repo_type}s/{namespace}/{name}/revision/{revision}` answers the same way for a branch, tag or commit.

**See:** [HuggingFace-Compatible API](./huggingface-compatible.md)

---

## List Repositories

**Pattern:** `GET /api/{repo_type}s?author={author}&limit=50&sort=recent`

**Examples:**
- `GET /api/models?author=username&limit=50`
- `GET /api/datasets?sort=trending`

**Query Parameters:**
- `author` (optional): Filter by namespace
- `limit` (default: 50, max: 100000): Max results
- `sort` (optional): Sort order
  - `"recent"` (default): Latest first
  - `"likes"`: Most liked
  - `"downloads"`: Most downloaded
  - `"trending"`: Trending algorithm (7-day activity)
- `fallback` (default: true): Enable external sources

**Response:**
```json
[
  {
    "id": "username/model-name",
    "author": "username",
    "private": false,
    "sha": "commit_hash",
    "lastModified": "2025-01-20T10:15:32Z",
    "createdAt": "2025-01-15T08:00:00Z",
    "downloads": 150,
    "likes": 25,
    "gated": false,
    "tags": []
  }
]
```

**Privacy Filtering:**
- **Public repos:** Visible to everyone
- **Private repos:** Only owner/org members
- **Anonymous:** Only public repos

---

## List User Repositories

**Pattern:** `GET /api/users/{username}/repos?limit=100&sort=recent`

**Query Parameters:**
- `limit` (default: 100, max: 100000): Max per type
- `sort` (optional): `recent`, `likes`, `downloads`
- `fallback` (default: true): Enable external sources

**Response:**
```json
{
  "models": [
    {
      "id": "username/model1",
      "author": "username",
      "private": false,
      "sha": "commit_hash",
      "lastModified": "2025-01-20T10:15:32Z",
      "createdAt": "2025-01-15T08:00:00Z",
      "downloads": 150,
      "likes": 25
    }
  ],
  "datasets": [...],
  "spaces": [...]
}
```

**Use Case:** User/org profile pages

---

## Usage Examples

### Create and Upload

```python
import requests

API_BASE = "http://localhost:28080/api"
TOKEN = "your_token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Create repository
create_resp = requests.post(
    f"{API_BASE}/repos/create",
    json={
        "type": "model",
        "name": "my-bert",
        "private": False
    },
    headers=HEADERS
)

repo_url = create_resp.json()["url"]
print(f"Created: {repo_url}")

# Upload files (see File Upload API for details)
```

---

### Move Repository to Organization

```python
# Transfer personal repo to organization
move_resp = requests.post(
    f"{API_BASE}/repos/move",
    json={
        "fromRepo": "alice/my-model",
        "toRepo": "my-org/my-model",
        "type": "model"
    },
    headers=HEADERS
)

result = move_resp.json()
print(f"Moved: {result['message']}")
print(f"New URL: {result['url']}")
```

---

### Squash Large Repository

```python
# Check current size
repo_resp = requests.get(
    f"{API_BASE}/models/username/large-model",
    headers=HEADERS
)
repo = repo_resp.json()
print(f"Current size: {repo['storage']['used_bytes']:,} bytes")
print(f"Commits: {repo.get('commit_count', 'N/A')}")

# Squash to reduce storage
squash_resp = requests.post(
    f"{API_BASE}/repos/squash",
    json={
        "repo": "username/large-model",
        "type": "model"
    },
    headers=HEADERS
)

result = squash_resp.json()
print(result["message"])

# Check new size
repo_resp = requests.get(
    f"{API_BASE}/models/username/large-model",
    headers=HEADERS
)
new_size = repo_resp.json()['storage']['used_bytes']
print(f"New size: {new_size:,} bytes")
```

---

### Delete Repository

```python
# Delete repository
delete_resp = requests.delete(
    f"{API_BASE}/repos/delete",
    json={
        "type": "dataset",
        "name": "old-dataset",
        "organization": None
    },
    headers=HEADERS
)

print(delete_resp.json()["message"])
```

---

## Best Practices

**Naming:**
- Use lowercase with hyphens: `my-model-v2`
- Avoid special characters
- Keep names descriptive and concise
- Check availability first

**Privacy:**
- Start public for community models
- Use private for work-in-progress
- Consider organization for team projects

**Organization:**
- Use organizations for multi-user projects
- Separate personal vs team repos
- Use consistent naming within orgs

**Maintenance:**
- Squash rarely-updated repos to save storage
- Move abandoned repos to archive org
- Delete truly unused repos
- Monitor storage usage

---

## Next Steps

- [File Upload API](./file-upload.md) - Upload files to repos
- [Branches API](./branches.md) - Branch management
- [Organizations API](./organizations.md) - Org management
- [Quota API](./quota.md) - Storage quotas
