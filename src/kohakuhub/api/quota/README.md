# Quota Management Module

## Overview

The quota management module provides comprehensive storage tracking and enforcement for KohakuHub users and organizations. It implements a dual-quota system that separately tracks and enforces limits for private and public repositories, allowing fine-grained control over resource allocation.

## Purpose

This module enables:

- Real-time monitoring of storage usage across all repositories
- Enforcement of storage limits to prevent resource exhaustion
- Separate quota management for private and public repositories
- Permission-based visibility of storage information
- Manual recalculation of storage usage when needed

## Key Features

### Dual-Quota System

The module implements separate quota tracking for:

- **Private Repositories**: Storage used by private repos with configurable limits
- **Public Repositories**: Storage used by public repos with independent limits
- **Unlimited Quotas**: Setting a quota to `None` allows unlimited storage

### Storage Calculation

Storage tracking includes:

- **Current Branch Storage**: All objects in the main branch
- **LFS Object Storage**: All Git LFS objects with version history
- **Deduplication Awareness**: Tracks both total and unique LFS storage
- **Incremental Updates**: Efficient delta updates for storage changes

### Permission-Based Access

The module provides different visibility levels:

- **Authenticated Access**: Users can view their own complete quota information
- **Organization Members**: Members can view organization quota details
- **Public Access**: Anyone can view public repository storage usage
- **Private Data Protection**: Private storage info only visible to authorized users

## Architecture

### Components

#### router.py

FastAPI router implementing REST endpoints for quota management:

- `GET /api/quota/{namespace}` - Get complete quota information (authenticated)
- `PUT /api/quota/{namespace}` - Set storage quotas (admin only)
- `POST /api/quota/{namespace}/recalculate` - Manually recalculate storage
- `GET /api/quota/{namespace}/public` - Get public quota info with conditional private data

**Data Models:**

- `QuotaInfo`: Complete quota information for authenticated users
- `PublicQuotaInfo`: Permission-aware quota data for public profiles
- `SetQuotaRequest`: Request body for setting quota limits

**Authorization:**

- Users can manage their own quotas
- Organization admins can manage org quotas
- Super-admins and admins have elevated permissions

#### util.py

Quota enforcement over the usage `kohakuhub.usage` keeps:

- `check_quota(namespace, additional_bytes, is_private, is_org)` - Validate quota before operations
- `get_storage_info(namespace, is_org)` - Retrieve current quota and usage
- `set_quota(namespace, private_quota_bytes, public_quota_bytes, is_org)` - Update quota limits

## Storage Management

### How Quotas Are Enforced

1. **Pre-upload Check**: `check_quota()` validates that new uploads won't exceed limits
2. **Kept Usage**: every change to a repository applies its difference to the repository's counters (`kohakuhub.usage`)
3. **Recount**: the `usage.recount` background task sets exact values and reports drift (admin Storage page)

### Storage Calculation Details

A repository uses its regular files on `main` (`main_regular_bytes`) plus
the LFS objects any branch's history links that are still stored, each
counted once (`lfs_bytes`); `used_bytes` is their sum. A namespace's usage is
the sum over its repositories, private and public apart, read with one
`GROUP BY` (`usage.namespace_usage`).

### Database Schema

The module expects these fields in User and Organization models:

```python
# Quota limits (None = unlimited)
private_quota_bytes: int | None
public_quota_bytes: int | None

# Current usage
private_used_bytes: int  # Default: 0
public_used_bytes: int   # Default: 0
```

## Usage Examples

### Check Quota Before Upload

```python
from kohakuhub.api.quota.util import check_quota

allowed, error_msg = check_quota(
    namespace="username",
    additional_bytes=100 * 1000 * 1000,  # 100 MB
    is_private=True,
    is_org=False
)

if not allowed:
    raise HTTPException(413, detail={"error": error_msg})
```

### How Usage Is Kept

Usage is not recomputed here: `kohakuhub.usage` keeps each repository's
counters up to date as it changes (commits on `main`, LFS history rows,
garbage collection), and a namespace's usage is summed from its repositories
(`usage.namespace_usage`). A recount (`usage.recount_repository`, or the
`usage.recount` background task for many) sets exact values:

```python
from kohakuhub import usage

usage.namespace_usage(["username"])  # {"username": {"private": ..., "public": ...}}
await usage.recount_repository(repo.id)  # one repository, exactly
usage.enqueue_recount("username")  # every repository of a namespace, in the background
```

### Get Storage Information

```python
from kohakuhub.api.quota.util import get_storage_info

info = get_storage_info("username", is_org=False)
print(f"Private quota: {info['private_quota_bytes']}")
print(f"Private used: {info['private_used_bytes']}")
print(f"Private available: {info['private_available_bytes']}")
print(f"Private percentage: {info['private_percentage_used']}%")
```

## API Endpoints

### Namespace Quotas
- `GET /api/quota/{namespace}` - Get complete quota information (authenticated)
- `PUT /api/quota/{namespace}` - Set storage quotas (admin only)
- `POST /api/quota/{namespace}/recalculate` - Manually recalculate storage
- `GET /api/quota/{namespace}/public` - Get public quota info with conditional private data
- `GET /api/quota/{namespace}/repos` - List storage for all repositories in a namespace

### Repository Quotas
- `GET /api/quota/repo/{repo_type}/{namespace}/{name}` - Get repository-specific quota information
- `PUT /api/quota/repo/{repo_type}/{namespace}/{name}` - Set repository-specific quota
- `POST /api/quota/repo/{repo_type}/{namespace}/{name}/recalculate` - Recalculate storage for a single repository

## Error Handling

The module uses HTTP exceptions for error conditions:

- `404 Not Found`: Namespace (user/org) does not exist
- `403 Forbidden`: User lacks permission to perform operation
- `413 Payload Too Large`: Upload would exceed quota (via check_quota)

## Integration Points

### Dependencies

- **Database Models**: User, Organization, Repository, LFSObjectHistory
- **LakeFS Client**: For retrieving object storage information
- **Authentication**: get_current_user, get_optional_user dependencies
- **Configuration**: cfg module for system settings

### Used By

- File upload handlers (check quota before accepting uploads)
- Repository management (create starts counting; delete and move need nothing: the sum follows)
- Commits, branch operations and garbage collection (`kohakuhub.usage` hooks)
- User/org profile pages (display quota information)

## Performance Considerations

- **No listing on the way**: a change costs in proportion to what it changed, not to the repository or namespace size
- **Summed on read**: namespace usage is one indexed `GROUP BY` over its repositories
- **Recounts page through LakeFS** (1000 objects per request), in the background

## Logging

The module uses the "QUOTA" logger for operational visibility:

- Info level: Quota changes, recalculation results
- Debug level: Incremental storage updates
- Warning level: Calculation failures (non-fatal)

## Future Enhancements

Potential improvements for consideration:

- Background job for periodic quota recalculation
- Quota usage alerts and notifications
- Historical usage tracking and analytics
- Per-repository quota limits
- Quota grace periods before enforcement
