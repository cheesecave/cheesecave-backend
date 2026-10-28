---
title: Settings API
description: Manage user, organization, and repository settings
icon: settings
---

# Settings API

Manage settings for users, organizations, and repositories including profiles, avatars, and LFS configuration.

## User Settings

### Get Current User Info

Get authenticated user information in HuggingFace-compatible format.

**Endpoint:** `GET /api/whoami-v2`

**Authentication:** Required

**Response:**

```json
{
  "type": "user",
  "id": "123",
  "name": "alice",
  "fullname": "alice",
  "avatarUrl": null,
  "email": "alice@example.com",
  "emailVerified": true,
  "canPay": false,
  "isPro": false,
  "orgs": [
    {
      "type": "org",
      "id": "7",
      "name": "myorg",
      "fullname": "myorg",
      "avatarUrl": null,
      "roleInOrg": "admin",
      "canPay": false,
      "isEnterprise": false
    }
  ],
  "auth": {
    "type": "access_token",
    "accessToken": {
      "displayName": "Auto-generated token",
      "role": "write"
    }
  },
  "site": {
    "name": "Example Hub",
    "api": "cheesecave",
    "version": "0.0.1",
    "build": { "git_sha": "12e65aa…", "build_time": "2026-10-27T12:15:55+00:00" }
  }
}
```

`site` is the same identity `GET /api/version` returns. `name` is the name of this installation (the site name the administrator set, else the
configured one; the product is always CheeseCave), `api` is `cheesecave`, `version` is the version of the backend package, and `build` says which code runs.

**How a client tells the kinds of hub apart** (what hfutils does with `/api/version`):

| `api` | `build` | Meaning |
|---|---|---|
| `cheesecave` | present | CheeseCave |
| `kohakuhub` | present | an older CheeseCave (before this identity change) |
| `kohakuhub` | absent | KohakuHub |

A KohakuHub server never sent `build`; the first CheeseCave builds still said `kohakuhub` but already sent it.

**Example:**

```python
import requests

response = requests.get(
    "http://localhost:28080/api/whoami-v2",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
user_info = response.json()
print(f"Logged in as: {user_info['name']}")
```

---

### Get Namespace Type

Determine if a namespace is a user or organization.

**Endpoint:** `GET /api/users/{username}/type`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username or organization name |
| `fallback` | boolean | query | No | Enable fallback to external sources (default: `true`) |

**Authentication:** Optional

**Response:**

```json
{
  "type": "user",
  "_source": "local"
}
```

Or:

```json
{
  "type": "org",
  "_source": "local"
}
```

**Example:**

```python
response = requests.get("http://localhost:28080/api/users/myorg/type")
data = response.json()
if data["type"] == "org":
    print("This is an organization")
```

---

### Get User Profile

Get public user profile information.

**Endpoint:** `GET /api/users/{username}/profile`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username to query |
| `fallback` | boolean | query | No | Enable fallback to external sources (default: `true`) |

**Authentication:** Optional

**Response:**

```json
{
  "username": "alice",
  "full_name": "Alice Smith",
  "bio": "ML Engineer",
  "website": "https://example.com",
  "social_media": {
    "twitter_x": "alice_ml",
    "github": "alice",
    "huggingface": "alice"
  },
  "created_at": "2025-01-01T00:00:00Z",
  "_source": "local"
}
```

**Example:**

```python
response = requests.get("http://localhost:28080/api/users/alice/profile")
profile = response.json()
```

---

### Update User Settings

Update authenticated user's settings.

**Endpoint:** `PUT /api/users/{username}/settings`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username (must match authenticated user) |

**Request Body:**

```json
{
  "email": "newemail@example.com",
  "full_name": "Alice Smith",
  "bio": "ML Engineer and researcher",
  "website": "https://example.com",
  "social_media": {
    "twitter_x": "alice_ml",
    "threads": "alice",
    "github": "alice",
    "huggingface": "alice"
  }
}
```

**All fields are optional:**

| Field | Type | Description |
|-------|------|-------------|
| `email` | string | Email address (triggers re-verification) |
| `full_name` | string | Full display name |
| `bio` | string | User biography |
| `website` | string | Personal website URL |
| `social_media` | object | Social media handles |

**Authentication:** Required (can only update own settings)

**Response:**

```json
{
  "success": true,
  "message": "User settings updated successfully"
}
```

**Example:**

```python
response = requests.put(
    "http://localhost:28080/api/users/alice/settings",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "bio": "ML Engineer and researcher",
        "website": "https://alice.example.com"
    }
)
```

---

### Manage User Avatar

#### Upload Avatar

**Endpoint:** `POST /api/users/{username}/avatar`

**Authentication:** Required (can only upload own avatar)

**Request:** Multipart form data with image file

Supported formats: JPEG, PNG, WebP, GIF
Maximum size: 10 MB

**Processing:**
- Images are resized to max 1024x1024 (maintains aspect ratio)
- Center-cropped to square
- Converted to JPEG (quality: 95)

**Response:**

```json
{
  "success": true,
  "message": "Avatar uploaded successfully",
  "size_bytes": 245678
}
```

#### Get Avatar

**Endpoint:** `GET /api/users/{username}/avatar`

**Authentication:** Optional

**Response:** JPEG image with cache headers

**Cache-Control:** `public, max-age=86400` (24 hours)

#### Delete Avatar

**Endpoint:** `DELETE /api/users/{username}/avatar`

**Authentication:** Required (can only delete own avatar)

**Response:**

```json
{
  "success": true,
  "message": "Avatar deleted successfully"
}
```

**Example:**

```python
# Upload avatar
with open("avatar.png", "rb") as f:
    response = requests.post(
        "http://localhost:28080/api/users/alice/avatar",
        headers={"Authorization": "Bearer YOUR_TOKEN"},
        files={"file": f}
    )

# Get avatar URL
avatar_url = "http://localhost:28080/api/users/alice/avatar"

# Delete avatar
response = requests.delete(
    "http://localhost:28080/api/users/alice/avatar",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
```

---

## Organization Settings

### Get Organization Profile

Get public organization profile information.

**Endpoint:** `GET /api/organizations/{org_name}/profile`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `org_name` | string | path | Yes | Organization name |

**Authentication:** Optional

**Response:**

```json
{
  "name": "myorg",
  "description": "Machine Learning Research",
  "bio": "We build open-source ML models",
  "website": "https://myorg.example.com",
  "social_media": {
    "twitter_x": "myorg_ml",
    "github": "myorg"
  },
  "member_count": 5,
  "created_at": "2025-01-01T00:00:00Z"
}
```

---

### Update Organization Settings

Update organization settings (admin only).

**Endpoint:** `PUT /api/organizations/{org_name}/settings`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `org_name` | string | path | Yes | Organization name |

**Request Body:**

```json
{
  "description": "Machine Learning Research",
  "bio": "We build open-source ML models",
  "website": "https://myorg.example.com",
  "social_media": {
    "twitter_x": "myorg_ml",
    "threads": "myorg",
    "github": "myorg",
    "huggingface": "myorg"
  }
}
```

**All fields are optional:**

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Organization description |
| `bio` | string | Organization biography |
| `website` | string | Organization website URL |
| `social_media` | object | Social media handles |

**Authentication:** Required (admin or super-admin role)

**Response:**

```json
{
  "success": true,
  "message": "Organization settings updated successfully"
}
```

**Example:**

```python
response = requests.put(
    "http://localhost:28080/api/organizations/myorg/settings",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "description": "Machine Learning Research",
        "website": "https://myorg.example.com"
    }
)
```

---

### Manage Organization Avatar

#### Upload Avatar

**Endpoint:** `POST /api/organizations/{org_name}/avatar`

**Authentication:** Required (admin or super-admin)

**Request:** Multipart form data with image file

Same processing as user avatars (1024x1024, center-cropped, JPEG).

**Response:**

```json
{
  "success": true,
  "message": "Avatar uploaded successfully",
  "size_bytes": 245678
}
```

#### Get Avatar

**Endpoint:** `GET /api/organizations/{org_name}/avatar`

**Authentication:** Optional

**Response:** JPEG image with cache headers

#### Delete Avatar

**Endpoint:** `DELETE /api/organizations/{org_name}/avatar`

**Authentication:** Required (admin or super-admin)

**Response:**

```json
{
  "success": true,
  "message": "Avatar deleted successfully"
}
```

**Example:**

```python
# Upload organization avatar
with open("org-logo.png", "rb") as f:
    response = requests.post(
        "http://localhost:28080/api/organizations/myorg/avatar",
        headers={"Authorization": "Bearer YOUR_TOKEN"},
        files={"file": f}
    )
```

---

## Repository Settings

### Update Repository Settings

Update repository configuration including visibility and LFS settings.

**Endpoint:** `PUT /api/{repo_type}s/{namespace}/{name}/settings`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `repo_type` | string | path | Yes | Repository type: `model`, `dataset`, or `space` |
| `namespace` | string | path | Yes | Repository namespace |
| `name` | string | path | Yes | Repository name |

**Request Body:**

```json
{
  "private": false,
  "lfs_threshold_bytes": 10000000,
  "lfs_keep_versions": 10,
  "lfs_suffix_rules": [".safetensors", ".bin", ".gguf"]
}
```

**All fields are optional:**

| Field | Type | Description | Constraints |
|-------|------|-------------|-------------|
| `private` | boolean | Repository visibility | Checks quota before changing |
| `gated` | `false`, `true`, `"auto"` or `"manual"` | Gated access. Gated repositories are not supported: `false` (or omitted) is accepted and changes nothing; `true`, `"auto"` and `"manual"` answer `501` and nothing in the request is applied | Any other string: `400` |
| `lfs_threshold_bytes` | integer | Size threshold for LFS (bytes) | Min: 1,000,000 (1 MB), null = server default |
| `lfs_keep_versions` | integer | Number of LFS versions to keep | Min: 2, null = server default |
| `lfs_suffix_rules` | array[string] | File extensions to always use LFS | Must start with '.', null = no custom rules |

**Note:** All size values use **decimal units** (1 MB = 1,000,000 bytes, not 1,048,576).

**Authentication:** Required (write permission)

**Response:**

```json
{
  "success": true,
  "message": "Repository settings updated successfully"
}
```

**Error Responses:**

| Status | Description |
|--------|-------------|
| 400 | Invalid settings or quota exceeded |
| 403 | Not authorized |
| 404 | Repository not found |

**Example:**

```python
# Update repository visibility and LFS settings
response = requests.put(
    "http://localhost:28080/api/models/myorg/mymodel/settings",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "private": False,
        "lfs_threshold_bytes": 10000000,  # 10 MB
        "lfs_keep_versions": 10,
        "lfs_suffix_rules": [".safetensors", ".bin", ".gguf"]
    }
)

# Make repository private
response = requests.put(
    "http://localhost:28080/api/models/myorg/mymodel/settings",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"private": True}
)

# Reset LFS threshold to server default
response = requests.put(
    "http://localhost:28080/api/models/myorg/mymodel/settings",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"lfs_threshold_bytes": None}
)
```

---

### Get Repository LFS Settings

Get repository LFS configuration with both configured and effective values.

**Endpoint:** `GET /api/{repo_type}s/{namespace}/{name}/settings/lfs`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `repo_type` | string | path | Yes | Repository type: `model`, `dataset`, or `space` |
| `namespace` | string | path | Yes | Repository namespace |
| `name` | string | path | Yes | Repository name |

**Authentication:** Required (write permission)

**Response:**

```json
{
  "lfs_threshold_bytes": 10000000,
  "lfs_keep_versions": 10,
  "lfs_suffix_rules": [".safetensors", ".bin"],
  "lfs_threshold_bytes_effective": 10000000,
  "lfs_threshold_bytes_source": "repository",
  "lfs_keep_versions_effective": 10,
  "lfs_keep_versions_source": "repository",
  "lfs_suffix_rules_effective": [
    ".safetensors", ".bin", ".pt", ".pth", ".ckpt",
    ".onnx", ".pb", ".h5", ".tflite", ".gguf", ".ggml",
    ".msgpack", ".zip", ".tar", ".gz", ".bz2", ".xz",
    ".7z", ".rar", ".npy", ".npz", ".arrow", ".parquet",
    ".mp4", ".avi", ".mkv", ".mov", ".wav", ".mp3",
    ".flac", ".tiff", ".tif"
  ],
  "lfs_suffix_rules_source": "merged",
  "server_defaults": {
    "lfs_threshold_bytes": 5000000,
    "lfs_keep_versions": 5,
    "lfs_suffix_rules_default": [
      ".safetensors", ".bin", ".pt", ".pth", ".ckpt",
      ".onnx", ".pb", ".h5", ".tflite", ".gguf", ".ggml",
      ".msgpack", ".zip", ".tar", ".gz", ".bz2", ".xz",
      ".7z", ".rar", ".npy", ".npz", ".arrow", ".parquet",
      ".mp4", ".avi", ".mkv", ".mov", ".wav", ".mp3",
      ".flac", ".tiff", ".tif"
    ]
  }
}
```

**Field Descriptions:**

- **Configured values** (null = using server default):
  - `lfs_threshold_bytes`: Repository-specific threshold
  - `lfs_keep_versions`: Repository-specific keep versions
  - `lfs_suffix_rules`: Repository-specific custom suffix rules

- **Effective values** (resolved with server defaults):
  - `*_effective`: Actual values being used
  - `*_source`: Where the value comes from (`repository` or `server_default` or `merged`)

- **Server defaults** (for reference):
  - Global default values from configuration
  - 32 built-in suffix rules for ML models, archives, and media files

**Important Notes:**

1. **Suffix rule merging**: Repository suffix rules are **ADDED TO** (not replacing) server defaults
2. **Server default suffixes**: Files with these extensions ALWAYS use LFS, even if size < threshold
3. **Size units**: All sizes use decimal units (1 MB = 1,000,000 bytes)

**Example:**

```python
response = requests.get(
    "http://localhost:28080/api/models/myorg/mymodel/settings/lfs",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
lfs_settings = response.json()

print(f"Threshold: {lfs_settings['lfs_threshold_bytes_effective']} bytes")
print(f"Keep versions: {lfs_settings['lfs_keep_versions_effective']}")
print(f"Custom suffixes: {lfs_settings['lfs_suffix_rules']}")
print(f"Total suffixes (with defaults): {len(lfs_settings['lfs_suffix_rules_effective'])}")
```

---

## Server Default LFS Suffix Rules

KohakuHub includes **32 server-wide default suffix rules** that always use LFS:

**ML Model Formats (12):**
`.safetensors`, `.bin`, `.pt`, `.pth`, `.ckpt`, `.onnx`, `.pb`, `.h5`, `.tflite`, `.gguf`, `.ggml`, `.msgpack`

**Compressed Archives (9):**
`.zip`, `.tar`, `.gz`, `.bz2`, `.xz`, `.7z`, `.rar`

**Data Files (4):**
`.npy`, `.npz`, `.arrow`, `.parquet`

**Media Files (5):**
`.mp4`, `.avi`, `.mkv`, `.mov`, `.wav`, `.mp3`, `.flac`

**Large Images (2):**
`.tiff`, `.tif`

These rules apply to ALL repositories automatically. Repository-specific suffix rules ADD to these defaults.

---

## Usage Examples

### Complete User Profile Update

```python
import requests

BASE_URL = "http://localhost:28080"
TOKEN = "YOUR_TOKEN"

headers = {"Authorization": f"Bearer {TOKEN}"}

# Update profile
requests.put(
    f"{BASE_URL}/api/users/alice/settings",
    headers=headers,
    json={
        "full_name": "Alice Smith",
        "bio": "ML Engineer specializing in NLP",
        "website": "https://alice.example.com",
        "social_media": {
            "twitter_x": "alice_ml",
            "github": "alice",
            "huggingface": "alice"
        }
    }
)

# Upload avatar
with open("avatar.jpg", "rb") as f:
    requests.post(
        f"{BASE_URL}/api/users/alice/avatar",
        headers=headers,
        files={"file": f}
    )
```

### Repository LFS Configuration

```python
# Configure repository to always use LFS for GGUF files
requests.put(
    f"{BASE_URL}/api/models/myorg/mymodel/settings",
    headers=headers,
    json={
        "lfs_threshold_bytes": 10000000,  # 10 MB
        "lfs_suffix_rules": [".gguf", ".ggml"]  # Added to 32 server defaults
    }
)

# Check effective settings
response = requests.get(
    f"{BASE_URL}/api/models/myorg/mymodel/settings/lfs",
    headers=headers
)
settings = response.json()

# All .safetensors files will use LFS (server default)
# All .gguf files will use LFS (custom rule)
# Files > 10 MB will use LFS (threshold)
```

---

## Next Steps

- [Organization Management API](./organizations.md) - Create and manage organizations
- [Quota Management API](./quota.md) - Manage storage quotas
- [Repository API](../API.md#repositories) - Create and manage repositories
- [CLI Settings Commands](../CLI.md#settings) - Manage settings via CLI
