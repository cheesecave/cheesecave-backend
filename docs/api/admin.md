---
title: Admin API
description: Comprehensive administrative endpoints for managing users, repos, quotas, storage
icon: i-carbon-security
---

# Admin API

Complete administrative control panel for KohakuHub. All endpoints require admin authentication.

Site name, footer introduction, header logo, and favicon settings are documented
in [Site Branding](../deployment/site-branding.md), including the public read
endpoint and admin text/upload/reset endpoints.

---

## Authentication

**All admin endpoints require:**
```
X-Admin-Token: your_admin_secret_token
```

**Configuration:**
```bash
CHEESE_CAVE_ADMIN_TOKEN=your_secret_token
```

**Example:**
```bash
curl -H "X-Admin-Token: secret" \
  http://localhost:28080/admin/api/users
```

### Browser sessions (the admin panel)

The panel exchanges the token once for a session cookie, so the token is not kept in the page and a reload does not sign the administrator out. Scripts keep using `X-Admin-Token`; when both are sent, the header is the one that is checked.

| Request | Answer |
|---|---|
| `POST /admin/api/session` `{"token": "..."}` | `201 {"expires_at", "absolute_expires_at"}` and the cookie `admin_session` (`Path=/admin`, `HttpOnly`, `SameSite=Strict`, `Secure` when the public address is https). The token goes through the same throttle as the header: `403` for a wrong one, `429` with `Retry-After` while the client has to wait. |
| `GET /admin/api/session` | `200` with the same fields when the cookie stands for a live session (the panel after a reload), `401` otherwise. |
| `DELETE /admin/api/session` | Signs out: the session is revoked on the server and the cookie cleared. |
| `GET /admin/api/admin-sessions` | The active sessions: `id`, `created_at`, `last_used_at`, `expires_at`, `absolute_expires_at`, `ip`, `user_agent` and `current` (the one asking). |
| `DELETE /admin/api/admin-sessions/{id}` | Ends one session (`404` when it is not active). Ending the one in use also clears the cookie. |
| `POST /admin/api/admin-sessions/revoke-others` | Ends every session but the one asking (all of them when called with the token header): `{"revoked": n}`. |

A session ends after 30 minutes without use (each use moves that forward) and at the latest 12 hours after it was made (`CHEESE_CAVE_ADMIN_SESSION_IDLE_MINUTES`, `CHEESE_CAVE_ADMIN_SESSION_ABSOLUTE_HOURS`), when it is revoked, and when the shared admin token is changed: every session carries a keyed fingerprint of the token it was made with. A cookie request that changes something (anything but `GET`, `HEAD`, `OPTIONS`) must also send `X-Requested-With: cheesecave-admin` and an `Origin` that is the public address of the hub or one of `CHEESE_CAVE_ADMIN_SESSION_ALLOWED_ORIGINS`; a page of another site can send neither, and the answer is `403`. A weak token or a disabled admin API closes the cookie as well (`503`). `CHEESE_CAVE_ADMIN_SESSION_ENABLED=false` turns sessions off: the session routes answer `404`, which is what an older backend answers, and the panel falls back to the token.

The cookie only travels under `/admin`, so the three repository actions the panel used to send to `/api/repos/*` with the token also exist as `DELETE /admin/api/repos/delete`, `POST /admin/api/repos/move` and `POST /admin/api/repos/squash`: the same bodies and the same handlers, called as the administrator.

---

## Users

### List All Users

**Pattern:** `GET /admin/api/users`

**Query Parameters:**
- `search` (optional): Search by username or email
- `limit` (default: 100): Max users to return
- `offset` (default: 0): Pagination offset
- `include_orgs` (default: false): Include organizations

**Response:**
```json
{
  "users": [
    {
      "id": 1,
      "username": "alice",
      "email": "alice@example.com",
      "email_verified": true,
      "is_active": true,
      "is_org": false,
      "private_quota_bytes": 10737418240,
      "public_quota_bytes": 5368709120,
      "private_used_bytes": 1073741824,
      "public_used_bytes": 536870912,
      "created_at": "2025-01-01T00:00:00Z"
    }
  ],
  "limit": 100,
  "offset": 0,
  "search": null
}
```

---

### Get User Details

**Pattern:** `GET /admin/api/users/{username}`

**Response:**
```json
{
  "id": 1,
  "username": "alice",
  "email": "alice@example.com",
  "email_verified": true,
  "is_active": true,
  "is_org": false,
  "private_quota_bytes": 10737418240,
  "public_quota_bytes": 5368709120,
  "private_used_bytes": 1073741824,
  "public_used_bytes": 536870912,
  "created_at": "2025-01-01T00:00:00Z"
}
```

---

### Create User

**Pattern:** `POST /admin/api/users`

**Request Body:**
```json
{
  "username": "bob",
  "email": "bob@example.com",
  "password": "secure_password",
  "email_verified": false,
  "is_active": true,
  "private_quota_bytes": null,
  "public_quota_bytes": null
}
```

**Response:** Same as Get User Details

**Status Codes:**
- `200 OK` - User created
- `400 Bad Request` - Username or email already exists

---

### Delete User

**Pattern:** `DELETE /admin/api/users/{username}?force={true|false}`

**Query Parameters:**
- `force` (default: false): Delete even if user owns repositories

**Response:**
```json
{
  "message": "User deleted: bob",
  "deleted_repositories": [
    "model:bob/my-model",
    "dataset:bob/my-dataset"
  ],
  "storage_cleanup": "scheduled"
}
```

The user, their repositories and all related rows are deleted in one
transaction. The same transaction schedules the storage cleanup that the
database cascade cannot do. Each repository's LakeFS repository and
`s3://{bucket}/{lakefs_repo}/` prefix are deleted by a
`storage.purge_repository` background task. LFS objects no remaining
repository references are deleted by `storage.collect_lfs`. Both show up
under **Background Tasks**, and both need a running `khub-worker`.
`storage_cleanup` is `"none"` when the user owned no repositories.

**Without Force:**
```json
{
  "error": "User owns repositories",
  "message": "User owns 2 repository(ies). Use force=true to delete user and their repositories.",
  "owned_repositories": [
    "model:bob/my-model",
    "dataset:bob/my-dataset"
  ]
}
```

**Status Codes:**
- `200 OK` - User deleted
- `400 Bad Request` - User owns repositories (without force)
- `404 Not Found` - User not found

---

### Set Email Verification

**Pattern:** `PATCH /admin/api/users/{username}/email-verification?verified={true|false}`

**Query Parameters:**
- `verified`: Email verification status

**Response:**
```json
{
  "username": "alice",
  "email": "alice@example.com",
  "email_verified": true
}
```

---

### Update User Quota

**Pattern:** `PUT /admin/api/users/{username}/quota`

**Request Body:**
```json
{
  "private_quota_bytes": 10737418240,
  "public_quota_bytes": 5368709120
}
```

**Response:**
```json
{
  "username": "alice",
  "private_quota_bytes": 10737418240,
  "public_quota_bytes": 5368709120,
  "private_used_bytes": 1073741824,
  "public_used_bytes": 536870912
}
```

---

### Password Reset Link

An account's owner who has forgotten the password sets a new one with a one-time link. The administrator makes the link here and hands it over out of band (chat, a call); nothing is sent by email, and the administrator never learns the password.

**Make a link:** `POST /admin/api/users/{username}/password-reset-link`

**Request Body** (optional; `ttl` is one of the fixed lifetimes below, nothing else is accepted):
```json
{ "ttl": "1h" }
```

| `ttl` | Lifetime |
|---|---|
| `15m` | 15 minutes |
| `1h` (default) | 1 hour |
| `6h` | 6 hours |
| `24h` | 24 hours |
| `3d` | 3 days |
| `7d` | 7 days |

**Response:**
```json
{
  "username": "alice",
  "url": "https://hub.example.com/reset-password#token=...",
  "ttl": "1h",
  "created_at": "2026-10-19T11:00:00Z",
  "expires_at": "2026-10-19T12:00:00Z"
}
```

The link is shown **once** and cannot be read again (only a hash of the token is stored). A new link replaces the unused one. The link works once; when it is used the account's sessions end (see [Reset a Password with a Link](authentication.md#reset-a-password-with-a-link)). It cannot be made for an unknown user (`404`), an organization or a disabled account (`400`).

**See whether a link is waiting:** `GET /admin/api/users/{username}/password-reset-link`
```json
{
  "active": true,
  "created_at": "2026-10-19T11:00:00Z",
  "expires_at": "2026-10-19T12:00:00Z",
  "default": "1h",
  "choices": [{ "key": "15m", "seconds": 900, "label": "15 minutes" }]
}
```
`choices` lists every lifetime, so a client does not have to copy the table.

**Withdraw it:** `DELETE /admin/api/users/{username}/password-reset-link` answers `{"revoked": 1}` (`0` when none was waiting).

**Choosing a lifetime:** short when the person is on a call with you (15 minutes, 1 hour), longer when they will read it later (6 hours, 24 hours), days only when they are away; the longer the link lives, the longer it can be misused if it is copied, so prefer the shortest that works and withdraw it if it is no longer needed. Comparable software ships similar defaults: Okta's reset link sent by an administrator lasts 1 hour, Devise's 6 hours, Auth0's password change ticket 5 days; Keycloak gives links an administrator sends a longer lifetime than the ones a user asks for.

---

## Repositories

### List All Repositories

**Pattern:** `GET /admin/api/repositories`

**Query Parameters:**
- `search` (optional): Search by full_id or name
- `repo_type` (optional): Filter by type (model/dataset/space)
- `namespace` (optional): Filter by namespace
- `limit` (default: 100): Max repos to return
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "repositories": [
    {
      "id": 1,
      "repo_type": "model",
      "namespace": "alice",
      "name": "my-model",
      "full_id": "alice/my-model",
      "private": false,
      "owner_id": 1,
      "owner_username": "alice",
      "created_at": "2025-01-01T00:00:00Z",
      "quota_bytes": null,
      "used_bytes": 536870912,
      "percentage_used": 50.0,
      "is_inheriting": true
    }
  ],
  "total": 150,
  "limit": 100,
  "offset": 0,
  "search": null
}
```

---

### Get Repository Details

**Pattern:** `GET /admin/api/repositories/{repo_type}/{namespace}/{name}`

**Response:**
```json
{
  "id": 1,
  "repo_type": "model",
  "namespace": "alice",
  "name": "my-model",
  "full_id": "alice/my-model",
  "lakefs_repo": "m-alice-my-model-3kx9f2c1q8w7e5r4t6y0u2",
  "private": false,
  "owner_id": 1,
  "owner_username": "alice",
  "created_at": "2025-01-01T00:00:00Z",
  "file_count": 15,
  "commit_count": 42,
  "total_size": 536870912,
  "quota_bytes": null,
  "used_bytes": 536870912,
  "percentage_used": 50.0,
  "is_inheriting": true
}
```

---

### Get Repository Files (Enhanced)

**Pattern:** `GET /admin/api/repositories/{repo_type}/{namespace}/{name}/files?ref=main`

**Query Parameters:**
- `ref` (default: "main"): Branch or commit reference

**Response:**
```json
{
  "files": [
    {
      "path": "model.safetensors",
      "size": 536870912,
      "checksum": "sha256:abc123...",
      "mtime": 1737360932,
      "is_lfs": true,
      "sha256": "abc123def456...",
      "version_count": 3
    },
    {
      "path": "config.json",
      "size": 512,
      "checksum": "abc123",
      "mtime": 1737360932,
      "is_lfs": false,
      "sha256": null,
      "version_count": 1
    }
  ],
  "ref": "main",
  "count": 2
}
```

**Enhanced Fields:**
- `version_count`: Number of versions tracked for this file path
- `is_lfs`: Whether file uses LFS (from database)
- `sha256`: LFS SHA256 (null for non-LFS files)

---

### Get Storage Breakdown

**Pattern:** `GET /admin/api/repositories/{repo_type}/{namespace}/{name}/storage-breakdown`

**Response:**
```json
{
  "regular_files_size": 10485760,
  "lfs_files_size": 526385152,
  "total_size": 536870912,
  "lfs_object_count": 15,
  "unique_lfs_objects": 10,
  "deduplication_savings": 175456051
}
```

**Fields:**
- `regular_files_size`: Total size of non-LFS files
- `lfs_files_size`: Total size of LFS files
- `lfs_object_count`: Number of LFS file records
- `unique_lfs_objects`: Unique LFS objects (by SHA256)
- `deduplication_savings`: Bytes saved by deduplication

---

### Recount Repository Storage

**Pattern:** `POST /admin/api/repositories/recalculate-all`

Schedules the `usage.recount` background task, which sets every repository's
exact storage usage and reports how far the kept usage had drifted.

**Query Parameters:**
- `namespace` (optional): Only this namespace's repositories

**Response:**
```json
{"task_id": 42, "already_pending": false}
```

`task_id` is `null` with `already_pending: true` when one is already scheduled.

### Storage Usage Recount

**Pattern:** `GET /admin/api/usage/recount` — the latest site-wide recount

**Response:**
```json
{
  "interval_hours": 0,
  "task": {
    "id": 42,
    "status": "succeeded",
    "progress_done": 120,
    "progress_total": 120,
    "stage": "recounting model:alice/demo",
    "stats": {"repositories": 120, "drifted": 2, "drift_bytes": 5242880},
    "drift": [{"repository": "model:alice/demo", "before": 10485760, "after": 5242880}],
    "created_at": "2026-09-30T10:00:00+00:00",
    "finished_at": "2026-09-30T10:02:13+00:00"
  }
}
```

`drift` lists the repositories whose usage was off by the most. `stats.busy`
counts repositories that changed while being recounted; each is recounted
again on its own.

**Pattern:** `POST /admin/api/usage/recount` — start one (`{"task_id", "already_pending"}`)

---

## Quota

### Get Quota Overview

**Pattern:** `GET /admin/api/quota/overview`

**Response:**
```json
{
  "users_over_quota": [
    {
      "username": "alice",
      "private_percentage": 105.2,
      "public_percentage": 95.0,
      "private_used": 11274289152,
      "private_quota": 10737418240,
      "public_used": 5100273459,
      "public_quota": 5368709120
    }
  ],
  "repos_over_quota": [
    {
      "full_id": "alice/huge-model",
      "repo_type": "model",
      "used_bytes": 2147483648,
      "quota_bytes": 1073741824,
      "percentage": 200.0
    }
  ],
  "top_consumers": [
    {
      "username": "alice",
      "is_org": false,
      "total_bytes": 16374562611
    }
  ],
  "system_storage": {
    "private_used": 107374182400,
    "public_used": 53687091200,
    "lfs_used": 161061273600,
    "total_used": 161061273600
  }
}
```

---

### Get Namespace Quota

**Pattern:** `GET /admin/api/quota/{namespace}?is_org={true|false}`

**Query Parameters:**
- `is_org` (default: false): Whether namespace is an organization

**Response:** Same as `/api/quota/{namespace}`

---

### Set Namespace Quota

**Pattern:** `PUT /admin/api/quota/{namespace}?is_org={true|false}`

**Request Body:**
```json
{
  "private_quota_bytes": 10737418240,
  "public_quota_bytes": 5368709120
}
```

**Response:** Updated quota info

---

### Recalculate Namespace Storage

**Pattern:** `POST /admin/api/quota/{namespace}/recalculate?is_org={true|false}`

Schedules a recount of the namespace's repositories (`usage.recount`); the
answer is the usage as it stands.

**Response:**
```json
{
  "namespace": "alice",
  "is_organization": false,
  "task_id": 43,
  "already_pending": false,
  "quota_bytes": 10737418240,
  "used_bytes": 1610612736,
  "available_bytes": 9126805504,
  "percentage_used": 15.0
}
```

---

## Storage

### Debug S3 Configuration

**Pattern:** `GET /admin/api/storage/debug`

**Response:**
```json
{
  "endpoint": "http://minio:9000",
  "bucket": "hub-storage",
  "region": "us-east-1",
  "force_path_style": true,
  "bucket_accessible": true,
  "head_bucket_response": "...",
  "test_Standard": {
    "success": true,
    "response_keys": ["Contents", "KeyCount"],
    "key_count": 10,
    "contents_count": 10,
    "sample_keys": ["lfs/ab/cd/abc123", "..."]
  },
  "test_With_Delimiter": {
    "success": true,
    "...": "..."
  }
}
```

**Use Case:** Diagnose S3 connectivity issues

---

### List S3 Buckets

**Pattern:** `GET /admin/api/storage/buckets`

**Response:**
```json
{
  "buckets": [
    {
      "name": "hub-storage",
      "creation_date": "2025-01-01T00:00:00Z",
      "total_size": 107374182400,
      "object_count": 2500
    }
  ]
}
```

---

### List S3 Objects

**Pattern:** `GET /admin/api/storage/objects/{bucket}?prefix={prefix}&limit=1000`

**Alternative:** `GET /admin/api/storage/objects?prefix={prefix}&limit=1000`

**Query Parameters:**
- `bucket` (optional): Bucket name (empty = use configured bucket)
- `prefix` (optional): Key prefix filter
- `limit` (default: 1000): Max objects to return

**Response:**
```json
{
  "objects": [
    {
      "key": "lfs/ab/cd/abc123def",
      "full_key": "lfs/ab/cd/abc123def",
      "size": 536870912,
      "last_modified": "2025-01-20T10:15:32Z",
      "storage_class": "STANDARD"
    }
  ],
  "bucket": "hub-storage",
  "is_truncated": false,
  "key_count": 1
}
```

---

### Delete Single Object

**Pattern:** `DELETE /admin/api/storage/objects/{key:path}`

**Path Parameter:**
- `key`: Full S3 object key (URL-decoded automatically)

**Example:**
```bash
DELETE /admin/api/storage/objects/lfs/ab/cd/abc123def
```

**Response:**
```json
{
  "success": true,
  "message": "Object deleted: lfs/ab/cd/abc123def"
}
```

---

### Prepare Prefix Deletion

**Pattern:** `POST /admin/api/storage/prefix/prepare-delete?prefix={prefix}`

**Query Parameters:**
- `prefix`: S3 prefix to delete

**Purpose:** Generate confirmation token for safe deletion

**Response:**
```json
{
  "confirm_token": "temp_token_abc123",
  "prefix": "lfs/ab/",
  "estimated_objects": 42,
  "expires_in": 60
}
```

**Token expires in 60 seconds**

---

### Delete Prefix (Confirmed)

**Pattern:** `DELETE /admin/api/storage/prefix?prefix={prefix}&confirm_token={token}`

**Query Parameters:**
- `prefix`: S3 prefix to delete
- `confirm_token`: Token from prepare-delete endpoint

**Security:**
- Requires confirmation token from prepare step
- Token expires in 60 seconds
- Prefix must match token

**Response:**
```json
{
  "success": true,
  "deleted_count": 42,
  "prefix": "lfs/ab/"
}
```

**Status Codes:**
- `200 OK` - Objects deleted
- `400 Bad Request` - Invalid/expired token or prefix mismatch

---

### List Orphaned LakeFS Repositories

**Pattern:** `GET /admin/api/storage/orphans`

This is a read-only audit of LakeFS repositories that no repository points at.
They are left behind by repositories deleted before storage cleanup was
scheduled on deletion, or by creates that failed halfway.

**Response:**
```json
{
  "orphans": [
    {
      "id": "m-bob-old-model-0k3j5h7g9f1d3s5a7p9o2i",
      "created_at": 1758000000,
      "storage_namespace": "s3://hub-storage/m-bob-old-model-0k3j5h7g9f1d3s5a7p9o2i",
      "purge_pending": false
    }
  ],
  "count": 1
}
```

`created_at` is the LakeFS creation time in Unix seconds. `purge_pending` is
true while a purge task is queued or running for that repository.

---

### Purge an Orphaned LakeFS Repository

**Pattern:** `POST /admin/api/storage/orphans/{lakefs_repo}/purge`

This schedules a `storage.purge_repository` background task, which deletes the
repository's S3 prefix and then the LakeFS repository. The task checks again
before deleting anything.

**Response:**
```json
{
  "lakefs_repo": "m-bob-old-model-0k3j5h7g9f1d3s5a7p9o2i",
  "task_id": 42,
  "already_pending": false
}
```

**Status Codes:**
- `200 OK` - Purge scheduled. `task_id` is null and `already_pending` true when one was already queued.
- `404 Not Found` - No such LakeFS repository
- `409 Conflict` - A repository points at it, so it is not an orphan

---

## Database

### List Database Tables

**Pattern:** `GET /admin/api/database/tables`

**Response:**
```json
{
  "tables": [
    {
      "name": "user",
      "model": "User",
      "columns": [
        {
          "name": "id",
          "type": "INT",
          "null": false,
          "unique": true
        },
        {
          "name": "username",
          "type": "VARCHAR",
          "null": false,
          "unique": true
        }
      ],
      "column_count": 15
    }
  ]
}
```

---

### Get Query Templates

**Pattern:** `GET /admin/api/database/templates`

**Response:**
```json
{
  "templates": [
    {
      "name": "List all users",
      "sql": "SELECT id, username, email, created_at FROM user LIMIT 100",
      "description": "Get first 100 users"
    },
    {
      "name": "Count repositories by type",
      "sql": "SELECT repo_type, COUNT(*) as count FROM repository GROUP BY repo_type",
      "description": "Repository type distribution"
    }
  ]
}
```

---

### Execute SQL Query

**Pattern:** `POST /admin/api/database/query`

**Request Body:**
```json
{
  "sql": "SELECT username, email, created_at FROM user WHERE email_verified = 1 LIMIT 10"
}
```

**Security:**
- **Only SELECT queries allowed**
- Row limit: 1000 max
- Timeout: 10 seconds
- Validates against dangerous operations

**Response:**
```json
{
  "columns": ["username", "email", "created_at"],
  "rows": [
    {
      "username": "alice",
      "email": "alice@example.com",
      "created_at": "2025-01-01T00:00:00Z"
    }
  ],
  "count": 1,
  "column_count": 3,
  "truncated": false
}
```

**Status Codes:**
- `200 OK` - Query executed
- `400 Bad Request` - Invalid query or execution failed

**Invalid Queries:**
- Non-SELECT statements (INSERT, UPDATE, DELETE, DROP, etc.)
- Multiple statements (`;` separator)
- Dangerous functions (SLEEP, LOAD_FILE, etc.)

---

## Search

### Global Search

**Pattern:** `GET /admin/api/search?q={query}&types=users,repos,commits&limit=20`

**Query Parameters:**
- `q`: Search query string
- `types` (default: ["users", "repos", "commits"]): Types to search
- `limit` (default: 20): Max results per type

**Response:**
```json
{
  "query": "alice",
  "results": {
    "users": [
      {
        "id": 1,
        "username": "alice",
        "email": "alice@example.com",
        "email_verified": true,
        "is_active": true
      }
    ],
    "repositories": [
      {
        "id": 1,
        "full_id": "alice/my-model",
        "repo_type": "model",
        "namespace": "alice",
        "name": "my-model",
        "private": false,
        "owner_username": "alice"
      }
    ],
    "commits": [
      {
        "id": 1,
        "commit_id": "abc123def",
        "message": "Initial commit",
        "username": "alice",
        "repo_full_id": "alice/my-model",
        "repo_type": "model",
        "branch": "main",
        "created_at": "2025-01-01T00:00:00Z"
      }
    ]
  },
  "result_counts": {
    "users": 1,
    "repositories": 1,
    "commits": 1
  }
}
```

---

## Statistics

### Basic Stats

**Pattern:** `GET /admin/api/stats`

**Response:**
```json
{
  "users": 150,
  "organizations": 25,
  "repositories": {
    "total": 500,
    "private": 200,
    "public": 300
  }
}
```

---

### Detailed Stats

**Pattern:** `GET /admin/api/stats/detailed`

**Response:**
```json
{
  "users": {
    "total": 150,
    "active": 145,
    "verified": 140,
    "inactive": 5
  },
  "organizations": {
    "total": 25
  },
  "repositories": {
    "total": 500,
    "private": 200,
    "public": 300,
    "by_type": {
      "model": 350,
      "dataset": 100,
      "space": 50
    }
  },
  "commits": {
    "total": 5000,
    "top_contributors": [
      {
        "username": "alice",
        "commit_count": 500
      }
    ]
  },
  "lfs": {
    "total_objects": 2500,
    "total_size": 107374182400
  },
  "storage": {
    "private_used": 53687091200,
    "public_used": 26843545600,
    "total_used": 80530636800
  }
}
```

---

### Time-Series Stats

**Pattern:** `GET /admin/api/stats/timeseries?days=30`

**Query Parameters:**
- `days` (default: 30, max: 365): Number of days to include

**Response:**
```json
{
  "repositories_by_day": {
    "2025-01-01": {
      "model": 5,
      "dataset": 2,
      "space": 1
    },
    "2025-01-02": {
      "model": 3,
      "dataset": 1,
      "space": 0
    }
  },
  "commits_by_day": {
    "2025-01-01": 15,
    "2025-01-02": 23
  },
  "users_by_day": {
    "2025-01-01": 2,
    "2025-01-02": 3
  }
}
```

**Use Case:** Generate charts and trends

---

### Top Repositories

**Pattern:** `GET /admin/api/stats/top-repos?limit=10&by=commits`

**Query Parameters:**
- `limit` (default: 10, max: 100): Number of repos
- `by`: Sort by `commits` or `size`

**Response:**
```json
{
  "top_repositories": [
    {
      "repo_full_id": "alice/popular-model",
      "repo_type": "model",
      "commit_count": 250,
      "private": false
    }
  ],
  "sorted_by": "commits"
}
```

---

### Overview

**Pattern:** `GET /admin/api/overview?sections=health,counts&range=7d`

One call for the admin Overview page: is everything running, what needs attention, what is happening. Read-only; same
`X-Admin-Token` as every admin route; the response carries `Cache-Control: no-store`.

**Query Parameters:**
- `sections` (optional): comma-separated names from `health`, `workers`, `queue`, `build`, `attention`, `counts`, `recent`,
  `activity`, `top`. Omitted means all of them; unknown names are ignored, so a newer client can ask for a section an older
  server lacks.
- `range` (default `7d`): `24h`, `7d` or `30d`. It sets the window of `counts.*.delta`/`series` and of `activity` (24 hourly
  buckets for `24h`, 7 or 30 daily buckets otherwise, always UTC). Any other value is a 422.

**Response:**
```json
{
  "schema": 1,
  "generated_at": "2026-10-19T14:30:02Z",
  "range": "7d",
  "sections": {
    "health": {
      "generated_at": "2026-10-19T14:29:50Z", "cache": {"hit": true, "age_s": 12},
      "status": "ok", "checked_at_ms": 1792420190000,
      "dependencies": [{"name": "postgres", "status": "ok", "latency_ms": 12, "detail": null}]
    },
    "workers": {"online": 3, "draining": 0, "lost": 1},
    "queue": {"queued": 5, "running": 1, "succeeded_24h": 425, "failed_24h": 2, "oldest_queued_age_s": 9,
              "hourly": [{"ok": 7, "failed": 0}]},
    "build": {"backend": "3fd3765"},
    "attention": {"items": [{
      "id": "tasks", "severity": "warning", "title": "2 tasks failed in the last 24 hours", "count": 2,
      "hint": "repo.recalc, lfs.gc",
      "link": {"path": "/tasks", "query": {"status": "failed", "window": "24h"}, "label": "Failed tasks"}
    }]},
    "counts": {
      "users": {"total": 25, "delta": 1, "series": [0, 0, 1, 0, 0, 0, 0]}, "orgs": 9,
      "repos": {"total": 502, "private": 71, "delta": 9, "series": []},
      "commits": {"total": 78701, "delta": 1420, "series": []},
      "storage": {"total": 48000000000000, "private": 6100000000000, "public": 41900000000000},
      "lfs": {"objects": 1130994, "size": 48700000000000}
    },
    "activity": {"range": "7d", "bucket": "day", "labels": ["10-13"], "commits": [], "repos": [], "users": []},
    "recent": {"items": [{"id": "commit:56", "kind": "commit", "actor": "alice",
                          "text": "committed “Fix tokenizer” to alice/clip", "at": "2026-10-19T14:25:00Z", "link": "/commits"}]},
    "top": {"by_commits": [{"repo_type": "dataset", "repo_full_id": "o/n", "commits": 5120}],
            "by_size": [{"repo_type": "dataset", "repo_full_id": "o/n", "size_bytes": 18200000000000}]}
  }
}
```

**Sections are independent.** Each carries its own `generated_at` and `cache` (`hit`, and `age_s` since it was computed).
One that fails is returned as `{"generated_at": "...", "error": {"code": "section_failed", "message": "..."}}` and the rest
are still returned with HTTP 200, so one failing probe never blanks the page. `schema` is an integer (currently 1); a client
that sees a larger one than it knows should fall back to the older endpoints.

**Notes:**
- `counts.*.total` are the same numbers as `/stats/detailed`. `delta` is the number of rows *created* within the range and
  `series` the number created per bucket, oldest first and zero-filled (not cumulative). Storage has no delta: nothing records
  past usage.
- `queue.hourly` is 24 consecutive hours ending now, built from the Tasks page's own 24 hour statistics, so "failed" and
  "backing up" mean the same thing in both places.
- `attention.items` are ordered by severity (`danger`, `warning`, `info`) and then by `id`; `link` is a page of the admin
  panel with its query parameters. The ids and their rules: `dependency` (a dependency is down), `worker` (a worker lost its
  heartbeat), `tasks` (tasks failed in 24 hours; `danger` once the failure rate reaches the Tasks page's critical threshold,
  else `warning`), `backlog` (the oldest due task has waited past the Tasks page's warn or critical threshold), `quota` (users
  or repositories over 100% of a quota), `lfs_drift` (the last LFS reconciliation task failed). Items are derived from data
  that is already cached or stored; nothing scans storage. Orphan LakeFS repositories are not an item: the scan is a manual
  action whose result is not kept.
- Computed values are cached in each server process (`health` 15 s, `workers`/`queue`/`attention` 5 s, `counts`/`recent` 30 s,
  `activity`/`top` 5 min, `build` for the life of the process) and concurrent requests for one value share one computation, so
  many open tabs do not multiply the dependency probes.
- `/stats`, `/stats/detailed`, `/stats/timeseries` and `/stats/top-repos` are unchanged.

---

## Fallback Sources

### List Fallback Sources

**Pattern:** `GET /admin/api/fallback-sources?namespace={namespace}&enabled={true|false}`

**Query Parameters:**
- `namespace` (optional): Filter by namespace
- `enabled` (optional): Filter by enabled status

**Response:**
```json
[
  {
    "id": 1,
    "namespace": "",
    "url": "https://huggingface.co",
    "token": "hf_xxx",
    "priority": 100,
    "name": "HuggingFace",
    "source_type": "huggingface",
    "enabled": true,
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
]
```

---

### Get Fallback Source

**Pattern:** `GET /admin/api/fallback-sources/{source_id}`

**Response:** Single fallback source object

**Status Codes:**
- `200 OK` - Source found
- `404 Not Found` - Source not found

---

### Create Fallback Source

**Pattern:** `POST /admin/api/fallback-sources`

**Request Body:**
```json
{
  "namespace": "",
  "url": "https://huggingface.co",
  "token": "hf_xxx",
  "priority": 100,
  "name": "HuggingFace",
  "source_type": "huggingface",
  "enabled": true
}
```

**Fields:**
- `namespace`: Empty string for global, or user/org name for namespace-specific
- `source_type`: Must be `"huggingface"` or `"kohakuhub"`
- `priority`: Higher = checked first (default: 100)

**Response:** Created source object

**Status Codes:**
- `200 OK` - Created
- `400 Bad Request` - Invalid source_type

---

### Update Fallback Source

**Pattern:** `PUT /admin/api/fallback-sources/{source_id}`

**Request Body:** (all fields optional)
```json
{
  "url": "https://huggingface.co",
  "token": "new_token",
  "priority": 50,
  "name": "HF Mirror",
  "source_type": "huggingface",
  "enabled": false
}
```

**Side Effect:** Clears fallback cache after update

**Response:** Updated source object

---

### Delete Fallback Source

**Pattern:** `DELETE /admin/api/fallback-sources/{source_id}`

**Side Effect:** Clears fallback cache after deletion

**Response:**
```json
{
  "success": true,
  "message": "Fallback source HuggingFace deleted"
}
```

---

### Get Cache Stats

**Pattern:** `GET /admin/api/fallback-sources/cache/stats`

**Response:**
```json
{
  "size": 42,
  "maxsize": 1000,
  "ttl_seconds": 3600,
  "usage_percent": 4.2
}
```

---

### Clear Cache

**Pattern:** `DELETE /admin/api/fallback-sources/cache/clear`

**Response:**
```json
{
  "success": true,
  "message": "Cache cleared (42 entries removed)",
  "old_size": 42
}
```

---

## Invitations

### Create Registration Invitation

**Pattern:** `POST /admin/api/invitations/register`

**Request Body:**
```json
{
  "org_id": null,
  "role": "member",
  "max_usage": -1,
  "expires_days": 30
}
```

**Fields:**
- `org_id`: Organization to join (null = no org)
- `role`: Role in organization (if org_id provided)
- `max_usage`:
  - `null`: One-time use
  - `-1`: Unlimited uses
  - `N`: Max N uses
- `expires_days`: Days until expiration

**Response:**
```json
{
  "success": true,
  "token": "invitation_token_abc123",
  "invitation_link": "http://localhost:28080/register?invitation=invitation_token_abc123",
  "expires_at": "2025-01-31T00:00:00Z",
  "max_usage": -1,
  "is_reusable": true,
  "action": "register_account"
}
```

---

### List All Invitations

**Pattern:** `GET /admin/api/invitations?action={action}&limit=100&offset=0`

**Query Parameters:**
- `action` (optional): Filter by type (`join_org`, `register_account`)
- `limit` (default: 100): Max invitations
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "invitations": [
    {
      "id": 1,
      "token": "token123",
      "action": "join_org",
      "org_id": 1,
      "org_name": "my-org",
      "role": "member",
      "email": "alice@example.com",
      "created_by_id": 1,
      "creator_username": "bob",
      "created_at": "2025-01-01T00:00:00Z",
      "expires_at": "2025-01-08T00:00:00Z",
      "max_usage": null,
      "usage_count": 0,
      "is_reusable": false,
      "is_available": true,
      "error_message": null,
      "used_at": null,
      "used_by_id": null,
      "used_by_username": null
    }
  ],
  "limit": 100,
  "offset": 0
}
```

---

### Delete Invitation

**Pattern:** `DELETE /admin/api/invitations/{token}`

**Response:**
```json
{
  "success": true,
  "message": "Invitation deleted successfully"
}
```

---

## Commits

### List Commits with Filters

**Pattern:** `GET /admin/api/commits?repo_full_id={repo_id}&username={username}&limit=100&offset=0`

**Query Parameters:**
- `repo_full_id` (optional): Filter by repository
- `username` (optional): Filter by author
- `limit` (default: 100): Max commits
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "commits": [
    {
      "id": 1,
      "commit_id": "abc123def456",
      "repo_full_id": "alice/my-model",
      "repo_type": "model",
      "branch": "main",
      "user_id": 1,
      "username": "alice",
      "message": "Update model",
      "description": "Improved accuracy",
      "created_at": "2025-01-20T10:15:32Z"
    }
  ],
  "limit": 100,
  "offset": 0
}
```

---

## Usage Examples

### Monitor Quota Usage

```python
import requests

ADMIN_TOKEN = "your_admin_token"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}
API_BASE = "http://localhost:28080/admin/api"

# Get quota overview
overview = requests.get(
    f"{API_BASE}/quota/overview",
    headers=ADMIN_HEADERS
).json()

# Alert for users over quota
for user in overview["users_over_quota"]:
    print(f"⚠️  {user['username']}: {user['private_percentage']}% used")

# Check top consumers
for consumer in overview["top_consumers"][:5]:
    print(f"Top: {consumer['username']} - {consumer['total_bytes']:,} bytes")
```

---

### Bulk Operations

```python
# Recount every repository's storage usage in the background
recalc_resp = requests.post(f"{API_BASE}/usage/recount", headers=ADMIN_HEADERS)
print(f"Recount task: {recalc_resp.json()['task_id']}")
```

---

### Search System

```python
# Search for user
search_resp = requests.get(
    f"{API_BASE}/search?q=alice&types=users,repos&limit=50",
    headers=ADMIN_HEADERS
)

results = search_resp.json()
print(f"Users: {results['result_counts']['users']}")
print(f"Repos: {results['result_counts']['repositories']}")
```

---

### Manage S3 Storage

```python
# List objects in lfs/ prefix
objects_resp = requests.get(
    f"{API_BASE}/storage/objects?prefix=lfs/ab/&limit=1000",
    headers=ADMIN_HEADERS
)

objects = objects_resp.json()["objects"]
print(f"Found {len(objects)} LFS objects")

# Delete old prefix (safe with confirmation)
prepare_resp = requests.post(
    f"{API_BASE}/storage/prefix/prepare-delete?prefix=old_folder/",
    headers=ADMIN_HEADERS
)

prep_data = prepare_resp.json()
print(f"Will delete {prep_data['estimated_objects']} objects")
print(f"Confirm token: {prep_data['confirm_token']}")

# Confirm deletion within 60 seconds
delete_resp = requests.delete(
    f"{API_BASE}/storage/prefix?prefix=old_folder/&confirm_token={prep_data['confirm_token']}",
    headers=ADMIN_HEADERS
)

print(f"Deleted {delete_resp.json()['deleted_count']} objects")
```

---

### Database Queries

```python
# Get active users
query_resp = requests.post(
    f"{API_BASE}/database/query",
    json={"sql": "SELECT username, email FROM user WHERE is_active = 1 LIMIT 100"},
    headers=ADMIN_HEADERS
)

data = query_resp.json()
print(f"Active users: {data['count']}")
for row in data["rows"]:
    print(f"  {row['username']}: {row['email']}")
```

---

## Security Best Practices

**For Admin Token:**
- Generate strong random token: `openssl rand -hex 32`
- Never commit to version control
- Rotate periodically
- Restrict network access to admin endpoints

**For Operations:**
- Always use confirmation tokens for destructive operations
- Test queries with LIMIT first
- Backup database before bulk changes
- Monitor storage before deletions

**Production Setup:**
- Restrict admin endpoints by IP (nginx)
- Use HTTPS only
- Enable audit logging
- Set up alerting for admin actions

---

## Next Steps

- [Quota API](./quota.md) - User quota management
- [Repositories API](./repositories.md) - Repository management
- [Statistics API](./stats.md) - System statistics
