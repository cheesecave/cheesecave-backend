---
title: Quota Management API
description: Manage storage quotas for users, organizations, and repositories
icon: database
---

# Quota Management API

Manage storage quotas at the namespace (user/organization) and repository levels. Track usage, set limits, and recalculate storage.

## Overview

KohakuHub supports three-level quota management:

1. **Namespace quotas**: Overall storage limits for users and organizations
   - Separate quotas for public and private repositories
   - Tracked per namespace (user or organization)

2. **Repository quotas**: Per-repository storage limits
   - Can override namespace quotas
   - Helps prevent single repo from consuming all space

3. **Quota inheritance**: Repositories inherit namespace quota by default

**All size values use decimal units** (1 MB = 1,000,000 bytes, not 1,048,576 bytes).

---

## Namespace Quota Endpoints

### Get Namespace Quota

Get storage quota information for a user or organization.

**Endpoint:** `GET /api/quota/{namespace}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `namespace` | string | path | Yes | Username or organization name |

**Authentication:** Required

**Response:**

```json
{
  "namespace": "alice",
  "is_organization": false,
  "quota_bytes": 107374182400,
  "used_bytes": 53687091200,
  "available_bytes": 53687091200,
  "percentage_used": 50.0
}
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `namespace` | string | Username or organization name |
| `is_organization` | boolean | Whether namespace is an organization |
| `quota_bytes` | integer | Total quota in bytes (null = unlimited) |
| `used_bytes` | integer | Storage currently used |
| `available_bytes` | integer | Remaining storage (null if unlimited) |
| `percentage_used` | float | Percentage of quota used (null if unlimited) |

**Example:**

```python
import requests

response = requests.get(
    "http://localhost:28080/api/quota/alice",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
quota = response.json()

print(f"Used: {quota['used_bytes'] / 1_000_000_000:.2f} GB")
print(f"Available: {quota['available_bytes'] / 1_000_000_000:.2f} GB")
print(f"Usage: {quota['percentage_used']:.1f}%")
```

---

### Set Namespace Quota

Set storage quota for a user or organization.

**Endpoint:** `PUT /api/quota/{namespace}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `namespace` | string | path | Yes | Username or organization name |

**Request Body:**

```json
{
  "quota_bytes": 107374182400
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `quota_bytes` | integer | Yes | Quota in bytes (null = unlimited) |

**Authorization:**
- Users: Can only set their own quota
- Organizations: Admin or super-admin members only

**Authentication:** Required

**Response:**

```json
{
  "namespace": "alice",
  "is_organization": false,
  "quota_bytes": 107374182400,
  "used_bytes": 53687091200,
  "available_bytes": 53687091200,
  "percentage_used": 50.0
}
```

**Example:**

```python
# Set quota to 100 GB (using decimal: 100 * 1000^3)
response = requests.put(
    "http://localhost:28080/api/quota/alice",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"quota_bytes": 100_000_000_000}
)

# Set unlimited quota
response = requests.put(
    "http://localhost:28080/api/quota/alice",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"quota_bytes": None}
)
```

---

### Recalculate Namespace Storage

Schedule a recount of a namespace's storage usage (the `usage.recount` background task). Usage is kept up to date as repositories change; the answer is the usage as it stands.

**Endpoint:** `POST /api/quota/{namespace}/recalculate`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `namespace` | string | path | Yes | Username or organization name |

**Authorization:**
- Users: Can only recalculate their own storage
- Organizations: Admin or super-admin members only

**Authentication:** Required

**Response:**

```json
{
  "namespace": "alice",
  "is_organization": false,
  "quota_bytes": 107374182400,
  "used_bytes": 53687091200,
  "available_bytes": 53687091200,
  "percentage_used": 50.0
}
```

**Example:**

```python
response = requests.post(
    "http://localhost:28080/api/quota/alice/recalculate",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
quota = response.json()
print(f"Recalculated storage: {quota['used_bytes']} bytes")
```

---

### Get Public Quota Info

Get public storage quota information with permission-based visibility.

**Endpoint:** `GET /api/quota/{namespace}/public`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `namespace` | string | path | Yes | Username or organization name |

**Authentication:** Optional

**Response (with permission):**

```json
{
  "namespace": "alice",
  "is_organization": false,
  "public_quota_bytes": 53687091200,
  "public_used_bytes": 10737418240,
  "public_available_bytes": 42949672960,
  "public_percentage_used": 20.0,
  "private_quota_bytes": 53687091200,
  "private_used_bytes": 21474836480,
  "private_available_bytes": 32212254720,
  "private_percentage_used": 40.0,
  "total_used_bytes": 32212254720,
  "can_see_private": true
}
```

**Response (without permission):**

```json
{
  "namespace": "alice",
  "is_organization": false,
  "public_quota_bytes": 53687091200,
  "public_used_bytes": 10737418240,
  "public_available_bytes": 42949672960,
  "public_percentage_used": 20.0,
  "total_used_bytes": 10737418240,
  "can_see_private": false
}
```

**Permission Rules:**

- **Public storage**: Always visible to everyone
- **Private storage**: Visible only if:
  - User viewing their own profile
  - Organization members viewing org profile

**Use Case:** Profile pages displaying storage usage

**Example:**

```python
# Anyone can view public quota
response = requests.get(
    "http://localhost:28080/api/quota/alice/public"
)
quota = response.json()

if quota["can_see_private"]:
    print(f"Total usage: {quota['total_used_bytes']} bytes")
    print(f"  Public: {quota['public_used_bytes']} bytes")
    print(f"  Private: {quota['private_used_bytes']} bytes")
else:
    print(f"Public usage: {quota['public_used_bytes']} bytes")
```

---

### List Repository Storage

Get detailed storage breakdown for all repositories in a namespace.

**Endpoint:** `GET /api/quota/{namespace}/repos`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `namespace` | string | path | Yes | Username or organization name |

**Authorization:**
- Users: Can only view their own repositories
- Organizations: Members only

**Authentication:** Required

**Response:**

```json
{
  "namespace": "alice",
  "is_organization": false,
  "total_repos": 3,
  "repositories": [
    {
      "repo_id": "alice/large-model",
      "repo_type": "model",
      "name": "large-model",
      "private": false,
      "quota_bytes": null,
      "used_bytes": 50000000000,
      "percentage_used": null,
      "is_inheriting": true,
      "created_at": "2025-01-01T00:00:00Z"
    },
    {
      "repo_id": "alice/small-dataset",
      "repo_type": "dataset",
      "name": "small-dataset",
      "private": true,
      "quota_bytes": 5000000000,
      "used_bytes": 2500000000,
      "percentage_used": 50.0,
      "is_inheriting": false,
      "created_at": "2025-01-10T00:00:00Z"
    }
  ]
}
```

**Notes:**

- Repositories are sorted by `used_bytes` descending (largest first)
- Includes both public and private repositories
- Shows which repos have custom quotas vs. inheriting

**Example:**

```python
response = requests.get(
    "http://localhost:28080/api/quota/alice/repos",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
data = response.json()

print(f"Total repositories: {data['total_repos']}")
print("\nLargest repositories:")
for repo in data["repositories"][:5]:
    size_gb = repo["used_bytes"] / 1_000_000_000
    status = "inheriting" if repo["is_inheriting"] else "custom quota"
    print(f"  {repo['repo_id']}: {size_gb:.2f} GB ({status})")
```

---

## Repository Quota Endpoints

### Get Repository Quota

Get storage quota information for a specific repository.

**Endpoint:** `GET /api/quota/repo/{repo_type}/{namespace}/{name}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `repo_type` | string | path | Yes | Repository type: `model`, `dataset`, or `space` |
| `namespace` | string | path | Yes | Repository namespace |
| `name` | string | path | Yes | Repository name |

**Authentication:** Optional (required for private repositories)

**Response:**

```json
{
  "repo_id": "alice/mymodel",
  "repo_type": "model",
  "namespace": "alice",
  "quota_bytes": 10000000000,
  "used_bytes": 5000000000,
  "available_bytes": 5000000000,
  "percentage_used": 50.0,
  "effective_quota_bytes": 10000000000,
  "namespace_quota_bytes": 107374182400,
  "namespace_used_bytes": 50000000000,
  "namespace_available_bytes": 57374182400,
  "is_inheriting": false
}
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `quota_bytes` | integer | Repository-specific quota (null if inheriting) |
| `used_bytes` | integer | Storage used by this repository |
| `available_bytes` | integer | Remaining storage for this repo |
| `percentage_used` | float | Percentage of repo quota used |
| `effective_quota_bytes` | integer | Actual enforced quota (repo or namespace) |
| `namespace_quota_bytes` | integer | Namespace-level quota |
| `namespace_used_bytes` | integer | Total namespace usage |
| `namespace_available_bytes` | integer | Remaining namespace quota |
| `is_inheriting` | boolean | Whether using namespace quota |

**Example:**

```python
response = requests.get(
    "http://localhost:28080/api/quota/repo/model/alice/mymodel",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
quota = response.json()

print(f"Repository quota: {quota['quota_bytes']}")
print(f"Used: {quota['used_bytes']} bytes ({quota['percentage_used']:.1f}%)")
print(f"Inheriting from namespace: {quota['is_inheriting']}")
```

---

### Set Repository Quota

Set a custom storage quota for a repository.

**Endpoint:** `PUT /api/quota/repo/{repo_type}/{namespace}/{name}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `repo_type` | string | path | Yes | Repository type: `model`, `dataset`, or `space` |
| `namespace` | string | path | Yes | Repository namespace |
| `name` | string | path | Yes | Repository name |

**Request Body:**

```json
{
  "quota_bytes": 10000000000
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `quota_bytes` | integer | Yes | Repository quota in bytes (null = inherit from namespace) |

**Constraints:**

- Repository quota cannot exceed namespace available quota
- Must have write permission to repository

**Authentication:** Required (write permission)

**Response:**

Same format as GET repository quota endpoint.

**Example:**

```python
# Set custom quota of 10 GB
response = requests.put(
    "http://localhost:28080/api/quota/repo/model/alice/mymodel",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"quota_bytes": 10_000_000_000}
)

# Reset to inherit from namespace
response = requests.put(
    "http://localhost:28080/api/quota/repo/model/alice/mymodel",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"quota_bytes": None}
)
```

---

### Recalculate Repository Storage

Recount a repository's storage usage exactly, from what it holds (it is kept up to date as the repository changes; this sets it from scratch).

**Endpoint:** `POST /api/quota/repo/{repo_type}/{namespace}/{name}/recalculate`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `repo_type` | string | path | Yes | Repository type: `model`, `dataset`, or `space` |
| `namespace` | string | path | Yes | Repository namespace |
| `name` | string | path | Yes | Repository name |

**Authentication:** Required (write permission)

**Response:**

Same format as GET repository quota endpoint.

**Example:**

```python
response = requests.post(
    "http://localhost:28080/api/quota/repo/model/alice/mymodel/recalculate",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
quota = response.json()
print(f"Recalculated: {quota['used_bytes']} bytes")
```

---

## Usage Examples

### Comprehensive Quota Management

```python
import requests

BASE_URL = "http://localhost:28080"
TOKEN = "YOUR_TOKEN"

headers = {"Authorization": f"Bearer {TOKEN}"}

class QuotaManager:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}

    # Namespace methods
    def get_namespace_quota(self, namespace: str):
        response = requests.get(
            f"{self.base_url}/api/quota/{namespace}",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def set_namespace_quota(self, namespace: str, quota_bytes: int = None):
        response = requests.put(
            f"{self.base_url}/api/quota/{namespace}",
            headers=self.headers,
            json={"quota_bytes": quota_bytes}
        )
        response.raise_for_status()
        return response.json()

    def recalculate_namespace(self, namespace: str):
        response = requests.post(
            f"{self.base_url}/api/quota/{namespace}/recalculate",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def list_repo_storage(self, namespace: str):
        response = requests.get(
            f"{self.base_url}/api/quota/{namespace}/repos",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    # Repository methods
    def get_repo_quota(self, repo_type: str, namespace: str, name: str):
        response = requests.get(
            f"{self.base_url}/api/quota/repo/{repo_type}/{namespace}/{name}",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def set_repo_quota(self, repo_type: str, namespace: str, name: str,
                      quota_bytes: int = None):
        response = requests.put(
            f"{self.base_url}/api/quota/repo/{repo_type}/{namespace}/{name}",
            headers=self.headers,
            json={"quota_bytes": quota_bytes}
        )
        response.raise_for_status()
        return response.json()

    def recalculate_repo(self, repo_type: str, namespace: str, name: str):
        response = requests.post(
            f"{self.base_url}/api/quota/repo/{repo_type}/{namespace}/{name}/recalculate",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    # Helper methods
    def format_bytes(self, bytes_val: int) -> str:
        """Format bytes in human-readable format."""
        if bytes_val is None:
            return "Unlimited"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_val < 1000.0:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1000.0
        return f"{bytes_val:.2f} PB"

    def print_quota_summary(self, namespace: str):
        """Print comprehensive quota summary."""
        # Get namespace quota
        quota = self.get_namespace_quota(namespace)
        print(f"\n=== Quota Summary: {namespace} ===")
        print(f"Type: {'Organization' if quota['is_organization'] else 'User'}")
        print(f"Quota: {self.format_bytes(quota['quota_bytes'])}")
        print(f"Used: {self.format_bytes(quota['used_bytes'])}")
        print(f"Available: {self.format_bytes(quota['available_bytes'])}")
        if quota['percentage_used']:
            print(f"Usage: {quota['percentage_used']:.1f}%")

        # Get repository breakdown
        repos_data = self.list_repo_storage(namespace)
        print(f"\n=== Repositories ({repos_data['total_repos']}) ===")
        for repo in repos_data["repositories"][:10]:  # Top 10
            size = self.format_bytes(repo["used_bytes"])
            status = "inheriting" if repo["is_inheriting"] else "custom"
            visibility = "private" if repo["private"] else "public"
            print(f"  {repo['repo_id']}: {size} ({visibility}, {status})")

# Usage
mgr = QuotaManager(BASE_URL, TOKEN)

# Set namespace quota to 100 GB
mgr.set_namespace_quota("alice", 100_000_000_000)

# Print summary
mgr.print_quota_summary("alice")

# Set custom quota for large model
mgr.set_repo_quota("model", "alice", "large-model", 50_000_000_000)

# Recalculate if needed
mgr.recalculate_namespace("alice")
```

### Monitor and Alert on Quota Usage

```python
def check_quota_alerts(namespace: str, token: str, threshold: float = 80.0):
    """Check if quota usage exceeds threshold and alert."""
    headers = {"Authorization": f"Bearer {token}"}

    # Check namespace quota
    response = requests.get(
        f"{BASE_URL}/api/quota/{namespace}",
        headers=headers
    )
    quota = response.json()

    alerts = []

    # Check overall usage
    if quota["percentage_used"] and quota["percentage_used"] > threshold:
        alerts.append({
            "type": "namespace",
            "name": namespace,
            "usage": quota["percentage_used"],
            "message": f"Namespace '{namespace}' is at {quota['percentage_used']:.1f}% capacity"
        })

    # Check individual repositories
    response = requests.get(
        f"{BASE_URL}/api/quota/{namespace}/repos",
        headers=headers
    )
    repos = response.json()["repositories"]

    for repo in repos:
        if repo["percentage_used"] and repo["percentage_used"] > threshold:
            alerts.append({
                "type": "repository",
                "name": repo["repo_id"],
                "usage": repo["percentage_used"],
                "message": f"Repository '{repo['repo_id']}' is at {repo['percentage_used']:.1f}% capacity"
            })

    return alerts

# Usage
alerts = check_quota_alerts("alice", TOKEN, threshold=80.0)
if alerts:
    print("⚠️  Quota Alerts:")
    for alert in alerts:
        print(f"  {alert['message']}")
else:
    print("✓ All quotas are healthy")
```

### Bulk Repository Quota Management

```python
def optimize_repository_quotas(namespace: str, token: str):
    """Automatically set repository quotas based on current usage."""
    headers = {"Authorization": f"Bearer {token}"}

    # Get all repositories
    response = requests.get(
        f"{BASE_URL}/api/quota/{namespace}/repos",
        headers=headers
    )
    repos = response.json()["repositories"]

    for repo in repos:
        # Only set quota for repos without custom quota
        if repo["is_inheriting"] and repo["used_bytes"] > 10_000_000_000:  # > 10 GB
            # Set quota to 150% of current usage (with some headroom)
            suggested_quota = int(repo["used_bytes"] * 1.5)

            print(f"Setting quota for {repo['repo_id']}: {suggested_quota / 1_000_000_000:.2f} GB")

            requests.put(
                f"{BASE_URL}/api/quota/repo/{repo['repo_type']}/{namespace}/{repo['name']}",
                headers=headers,
                json={"quota_bytes": suggested_quota}
            )

# Usage
optimize_repository_quotas("alice", TOKEN)
```

---

## JavaScript/TypeScript Example

```javascript
class QuotaAPI {
  constructor(baseURL, token) {
    this.baseURL = baseURL;
    this.headers = {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    };
  }

  async getNamespaceQuota(namespace) {
    const response = await fetch(
      `${this.baseURL}/api/quota/${namespace}`,
      { headers: this.headers }
    );
    return await response.json();
  }

  async setNamespaceQuota(namespace, quotaBytes) {
    const response = await fetch(
      `${this.baseURL}/api/quota/${namespace}`,
      {
        method: 'PUT',
        headers: this.headers,
        body: JSON.stringify({ quota_bytes: quotaBytes })
      }
    );
    return await response.json();
  }

  async listRepoStorage(namespace) {
    const response = await fetch(
      `${this.baseURL}/api/quota/${namespace}/repos`,
      { headers: this.headers }
    );
    return await response.json();
  }

  async getRepoQuota(repoType, namespace, name) {
    const response = await fetch(
      `${this.baseURL}/api/quota/repo/${repoType}/${namespace}/${name}`,
      { headers: this.headers }
    );
    return await response.json();
  }

  formatBytes(bytes) {
    if (!bytes) return 'Unlimited';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = bytes;
    let unitIndex = 0;
    while (size >= 1000 && unitIndex < units.length - 1) {
      size /= 1000;
      unitIndex++;
    }
    return `${size.toFixed(2)} ${units[unitIndex]}`;
  }
}

// Usage
const quotaAPI = new QuotaAPI('http://localhost:28080', 'YOUR_TOKEN');

const quota = await quotaAPI.getNamespaceQuota('alice');
console.log(`Used: ${quotaAPI.formatBytes(quota.used_bytes)}`);
console.log(`Available: ${quotaAPI.formatBytes(quota.available_bytes)}`);

const repos = await quotaAPI.listRepoStorage('alice');
console.log(`Total repositories: ${repos.total_repos}`);
```

---

## CLI Usage

See [CLI Documentation](../CLI.md#quota) for command-line interface:

```bash
# Get namespace quota
kohub-cli quota get alice

# Set namespace quota (100 GB)
kohub-cli quota set alice --quota 100000000000

# List repository storage
kohub-cli quota repos alice

# Get repository quota
kohub-cli quota repo get model alice/mymodel

# Set repository quota (10 GB)
kohub-cli quota repo set model alice/mymodel --quota 10000000000

# Recalculate storage
kohub-cli quota recalculate alice
```

---

## Next Steps

- [Repository Settings API](./settings.md) - Configure repository visibility
- [Statistics API](./stats.md) - Track repository downloads and activity
- [Organization Management API](./organizations.md) - Manage organizations
