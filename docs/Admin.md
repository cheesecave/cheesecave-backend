# Admin Portal Guide

*Complete guide to KohakuHub's administration interface*

**Last Updated:** January 2025
**Access:** http://your-hub.com/admin

---

## Table of Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Dashboard](#dashboard)
4. [User Management](#user-management)
5. [Repository Management](#repository-management)
6. [Commit History Viewer](#commit-history-viewer)
7. [S3 Storage Browser](#s3-storage-browser)
8. [Quota Management](#quota-management)
9. [Invitation Management](#invitation-management)
10. [API Reference](#api-reference)
11. [Security Best Practices](#security-best-practices)

---

## Overview

The Admin Portal provides a centralized interface for managing your KohakuHub instance. It offers:

- **User Management** - Create, view, and delete users
- **Repository Browser** - View all repositories with statistics
- **Commit History** - Track commits across all repositories
- **Storage Browser** - Browse S3 buckets and objects
- **Quota Management** - Set and monitor storage quotas
- **Invitation Management** - Create and manage registration invitations
- **Statistics Dashboard** - Real-time insights into usage
- **Bulk Operations** - Recalculate storage for all repositories

**Access URL:**
```
http://your-hub.com/admin
```

---

## Authentication

### Admin Token

The admin portal requires a secret token configured in your environment:

**Configuration:**
```yaml
# docker-compose.yml
environment:
  KOHAKU_HUB_ADMIN_ENABLED: "true"
  KOHAKU_HUB_ADMIN_SECRET_TOKEN: "your-secret-token-here"  # CHANGE THIS!
```

**Security:**
- ⚠️ **NEVER** use default token `"change-me-in-production"` in production
- ✅ Generate strong random token: `openssl rand -hex 32`
- ✅ Store securely (environment variable, secrets manager)
- ✅ Rotate regularly
- ✅ Use HTTPS in production

### Login

1. Navigate to `/admin`
2. Enter your admin secret token
3. Token is stored in browser session (not localStorage for security)
4. Auto-logout on browser close

**Example:**
```bash
# Generate secure token
openssl rand -hex 32
# Output: a1b2c3d4e5f6...

# Add to docker-compose.yml
KOHAKU_HUB_ADMIN_SECRET_TOKEN: "a1b2c3d4e5f6..."

# Restart
docker-compose up -d
```

---

## Dashboard

### Overview Statistics

The dashboard shows real-time statistics from your database:

**User Stats:**
- Total users
- Active users
- Email verified users
- Inactive users

**Organization Stats:**
- Total organizations

**Repository Stats:**
- Total repositories
- Private vs public repositories
- Breakdown by type (models, datasets, spaces)

**Commit Stats:**
- Total commits
- Top contributors (by commit count)

**Storage Stats:**
- Total storage used (private + public)
- Private vs public storage
- LFS object count and size

**Quick Actions:**
- Navigate to user management
- Browse repositories
- View commits
- Inspect S3 storage
- Manage quotas
- Manage invitations

---

## User Management

### List Users

**Features:**
- View all users with pagination
- Sort by ID, username, storage usage
- Filter and search
- Storage quota visualization

**Columns:**
- ID, Username, Email
- Private storage (used/quota)
- Public storage (used/quota)
- Total storage
- Email verification status
- Active status
- Created date

### Create User

**Fields:**
- Username (required, unique)
- Email (required, unique)
- Password (required)
- Email verified (checkbox)
- Is active (checkbox)
- Private quota (bytes, optional = unlimited)
- Public quota (bytes, optional = unlimited)

**Example:**
```
Username: alice
Email: alice@example.com
Password: ********
Email Verified: ✓
Is Active: ✓
Private Quota: 10737418240  (10 GB)
Public Quota: 53687091200   (50 GB)
```

**API Endpoint:**
```bash
curl -X POST http://localhost:48888/admin/api/users \
  -H "X-Admin-Token: your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice",
    "email": "alice@example.com",
    "password": "secure_password",
    "email_verified": true,
    "is_active": true,
    "private_quota_bytes": 10737418240,
    "public_quota_bytes": 53687091200
  }'
```

### View User Details

Click "View" to see:
- User ID, username, email
- Verification and active status
- Storage quotas (private, public)
- Storage used (private, public)
- Created date

**Actions:**
- Manage Quota (navigate to quota page)

### Delete User

**Normal Delete:**
- Deletes user account
- Deletes all sessions and tokens
- Deletes organization memberships
- **Keeps** repositories (must delete separately)

**Force Delete:**
- Deletes everything above
- **Also deletes** all owned repositories
- Their LakeFS repositories, S3 data and LFS objects no other repository uses
  are removed by background tasks scheduled in the same step; follow them
  under **Background Tasks** (a `khub-worker` must be running)
- ⚠️ Cannot be undone!

**Workflow:**
1. Click "Delete" → Confirmation dialog
2. If user owns repos → Shows repo list
3. Choose: Cancel or Force Delete
4. Confirm force delete → All data deleted

**API Endpoint:**
```bash
# Normal delete (fails if user owns repos)
curl -X DELETE http://localhost:48888/admin/api/users/alice \
  -H "X-Admin-Token: your-secret-token"

# Force delete (deletes user and all their repos)
curl -X DELETE "http://localhost:48888/admin/api/users/alice?force=true" \
  -H "X-Admin-Token: your-secret-token"
```

### Toggle Email Verification

**Use case:** Manually verify users when email verification is disabled or failed.

**Action:** Click "Verify" or "Unverify" button → Instant update

**API Endpoint:**
```bash
curl -X PATCH http://localhost:48888/admin/api/users/alice/email-verification?verified=true \
  -H "X-Admin-Token: your-secret-token"
```

---

## Repository Management

### List Repositories

**Filters:**
- Repository type (model/dataset/space)
- Namespace (user or organization)

**Columns:**
- ID
- Type (color-coded badge)
- Full repository ID (namespace/name)
- Privacy status (Private/Public badge)
- Owner username
- Storage quota and usage
- Created date

**Actions:**
- View Details → Opens detailed dialog

### Repository Details

**Information:**
- ID, Type, Full ID
- Namespace, Name
- Owner username
- Privacy status
- Created date
- **File count** (from database, active files only)
- **Commit count** (from database)
- **Total size** (sum of all active files)
- **Quota information** (quota, used, percentage, inheriting status)

**Actions:**
- View in Main App → Opens repository in main UI

**API Endpoint:**
```bash
curl http://localhost:48888/admin/api/repositories/model/org/my-model \
  -H "X-Admin-Token: your-secret-token"
```

---

## Commit History Viewer

### Overview

View all commits across all repositories in your instance.

**Filters:**
- Repository ID (e.g., "org/repo-name")
- Author username

**Columns:**
- Commit ID (first 8 chars)
- Repository (type badge + full ID)
- Branch
- Author
- Message (truncated, hover for full)
- Created date

**Sorting:**
- Sort by ID, created date, username, repository

**Pagination:**
- Page size: 10, 20, 50, 100
- Navigate through pages

**API Endpoint:**
```bash
# List all commits
curl http://localhost:48888/admin/api/commits?limit=100 \
  -H "X-Admin-Token: your-secret-token"

# Filter by repository
curl "http://localhost:48888/admin/api/commits?repo_full_id=org/model&limit=50" \
  -H "X-Admin-Token: your-secret-token"

# Filter by author
curl "http://localhost:48888/admin/api/commits?username=alice&limit=50" \
  -H "X-Admin-Token: your-secret-token"
```

### Use Cases

- Track user activity
- Find specific commits
- Monitor repository changes
- Debug commit issues
- Audit trail

---

## S3 Storage Browser

### Orphaned LakeFS repositories

The **Storage** page ends with an audit of LakeFS repositories that no
repository points at.
- **Scan** lists each orphan with its creation time and storage namespace.
- **Purge** schedules a background task that deletes its S3 prefix and then
  the LakeFS repository.
- **Purge all** does the same for every listed orphan.

Nothing is deleted until you confirm.

### Bucket List

**Overview:**
- View all S3 buckets
- Total size and object count
- Visual progress bars
- Creation dates

**Metrics:**
- Bucket name
- Total size (formatted: KB, MB, GB, TB)
- Object count
- Creation date
- Progress bar (relative to 100GB)

**Actions:**
- Click bucket → Browse contents

**API Endpoint:**
```bash
curl http://localhost:48888/admin/api/storage/buckets \
  -H "X-Admin-Token: your-secret-token"
```

**Response:**
```json
{
  "buckets": [
    {
      "name": "hub-storage",
      "creation_date": "2025-01-01T00:00:00Z",
      "total_size": 107374182400,
      "object_count": 5000
    }
  ]
}
```

### Object Browser

**Features:**
- List objects in selected bucket
- Filter by prefix (e.g., "lfs/", "models/")
- Pagination (up to 1000 objects)

**Columns:**
- Key (full S3 path)
- Size
- Storage class (STANDARD, etc.)
- Last modified date

**Prefix Filtering:**
```
Enter prefix: lfs/
→ Shows only objects starting with "lfs/"

Enter prefix: hf-model-org-repo/
→ Shows objects for specific repository
```

**API Endpoint:**
```bash
# List objects in bucket
curl "http://localhost:48888/admin/api/storage/objects/hub-storage?prefix=lfs/&limit=100" \
  -H "X-Admin-Token: your-secret-token"
```

---

## Quota Management

### View Quota

**Per-user or per-organization:**
- Private quota (limit)
- Private used
- Public quota (limit)
- Public used
- Total usage
- Usage percentages

**API Endpoint:**
```bash
# Get user quota
curl "http://localhost:48888/admin/api/quota/alice?is_org=false" \
  -H "X-Admin-Token: your-secret-token"

# Get organization quota
curl "http://localhost:48888/admin/api/quota/my-org?is_org=true" \
  -H "X-Admin-Token: your-secret-token"
```

**Response:**
```json
{
  "namespace": "alice",
  "is_organization": false,
  "private_quota_bytes": 10737418240,
  "public_quota_bytes": 53687091200,
  "private_used_bytes": 1234567890,
  "public_used_bytes": 5678901234,
  "private_available_bytes": 9502850350,
  "public_available_bytes": 47008189966,
  "private_percentage_used": 11.5,
  "public_percentage_used": 10.6,
  "total_used_bytes": 6913469124
}
```

### Set Quota

**Fields:**
- Private quota bytes (null = unlimited)
- Public quota bytes (null = unlimited)

**Examples:**
```
10 GB = 10737418240 bytes
50 GB = 53687091200 bytes
Unlimited = (empty/null)
```

**API Endpoint:**
```bash
curl -X PUT http://localhost:48888/admin/api/quota/alice \
  -H "X-Admin-Token: your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "private_quota_bytes": 10737418240,
    "public_quota_bytes": 53687091200
  }'
```

### Recalculate Storage

**Purpose:** Re-scan all files and update storage usage.

**When to use:**
- Database out of sync
- After manual S3 operations
- Quota shows incorrect values

**Process:**
1. Scans all files for namespace
2. Sums file sizes (private and public separately)
3. Updates User/Organization table

**API Endpoint:**
```bash
curl -X POST "http://localhost:48888/admin/api/quota/alice/recalculate?is_org=false" \
  -H "X-Admin-Token: your-secret-token"
```

### Bulk Storage Recalculation

**NEW:** Recalculate storage for all repositories at once.

**API Endpoint:**
```bash
# Recalculate all repositories
curl -X POST http://localhost:48888/admin/api/repositories/recalculate-all \
  -H "X-Admin-Token: your-secret-token"

# Filter by type
curl -X POST "http://localhost:48888/admin/api/repositories/recalculate-all?repo_type=model" \
  -H "X-Admin-Token: your-secret-token"

# Filter by namespace
curl -X POST "http://localhost:48888/admin/api/repositories/recalculate-all?namespace=org" \
  -H "X-Admin-Token: your-secret-token"
```

**Response:**
```json
{
  "total": 250,
  "success_count": 248,
  "failure_count": 2,
  "failures": [
    {
      "repo_id": "org/problem-repo",
      "error": "Repository not found in LakeFS"
    }
  ],
  "message": "Recalculated storage for 248/250 repositories"
}
```

---

## Invitation Management

### Create Registration Invitation

**Purpose:** Generate invitations for user registration (useful for invite-only mode).

**Features:**
- Optional organization membership after registration
- Reusable invitations with usage limits
- Configurable expiration

**API Endpoint:**
```bash
curl -X POST http://localhost:48888/admin/api/invitations/register \
  -H "X-Admin-Token: your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": null,
    "role": "member",
    "max_usage": 10,
    "expires_days": 30
  }'
```

**Response:**
```json
{
  "success": true,
  "token": "abc123xyz...",
  "invitation_link": "http://your-hub.com/register?invitation=abc123xyz...",
  "expires_at": "2025-02-14T12:00:00Z",
  "max_usage": 10,
  "is_reusable": true,
  "action": "register_account"
}
```

**Invitation Types:**
- **One-time:** `max_usage: null` - Single use invitation
- **Limited:** `max_usage: 10` - Can be used 10 times
- **Unlimited:** `max_usage: -1` - Unlimited uses

**Auto-join Organization:**
```json
{
  "org_id": 5,
  "role": "member",
  "max_usage": 50,
  "expires_days": 90
}
```

Users who register with this invitation will automatically join the organization as members.

### List All Invitations

**API Endpoint:**
```bash
# List all invitations
curl http://localhost:48888/admin/api/invitations \
  -H "X-Admin-Token: your-secret-token"

# Filter by action type
curl "http://localhost:48888/admin/api/invitations?action=register_account" \
  -H "X-Admin-Token: your-secret-token"
```

**Response:**
```json
{
  "invitations": [
    {
      "id": 1,
      "token": "abc123...",
      "action": "register_account",
      "org_id": null,
      "org_name": null,
      "role": null,
      "email": null,
      "created_by": 1,
      "creator_username": "System",
      "created_at": "2025-01-15T12:00:00Z",
      "expires_at": "2025-02-15T12:00:00Z",
      "max_usage": 10,
      "usage_count": 5,
      "is_reusable": true,
      "is_available": true,
      "error_message": null,
      "used_at": null,
      "used_by": null
    }
  ],
  "limit": 100,
  "offset": 0
}
```

### Delete Invitation

**API Endpoint:**
```bash
curl -X DELETE http://localhost:48888/admin/api/invitations/{token} \
  -H "X-Admin-Token: your-secret-token"
```

---

## API Reference

### Authentication

All admin API endpoints require `X-Admin-Token` header:

```bash
curl -H "X-Admin-Token: your-secret-token" \
  http://localhost:48888/admin/api/stats
```

### Endpoints Overview

**User Management:**
```
GET    /admin/api/users                    # List users
GET    /admin/api/users/{username}         # Get user info
POST   /admin/api/users                    # Create user
DELETE /admin/api/users/{username}         # Delete user
PATCH  /admin/api/users/{username}/email-verification  # Set verification
```

**Repository Management:**
```
GET /admin/api/repositories                # List repositories
GET /admin/api/repositories/{type}/{namespace}/{name}  # Get details
POST /admin/api/repositories/recalculate-all           # Bulk storage recalc
```

**Commit History:**
```
GET /admin/api/commits                     # List commits
```

**Storage:**
```
GET /admin/api/storage/buckets             # List buckets
GET /admin/api/storage/objects/{bucket}    # List objects
```

**Statistics:**
```
GET /admin/api/stats                       # Basic stats
GET /admin/api/stats/detailed              # Detailed stats
GET /admin/api/stats/timeseries?days=30    # Time-series data
GET /admin/api/stats/top-repos?by=commits  # Top repositories
```

**Quota:**
```
GET  /admin/api/quota/{namespace}          # Get quota
PUT  /admin/api/quota/{namespace}          # Set quota
POST /admin/api/quota/{namespace}/recalculate  # Recalculate
```

**Invitations:**
```
POST   /admin/api/invitations/register     # Create registration invitation
GET    /admin/api/invitations              # List all invitations
DELETE /admin/api/invitations/{token}      # Delete invitation
```

### Response Formats

**User Info:**
```json
{
  "id": 1,
  "username": "alice",
  "email": "alice@example.com",
  "email_verified": true,
  "is_active": true,
  "private_quota_bytes": 10737418240,
  "public_quota_bytes": 53687091200,
  "private_used_bytes": 1234567,
  "public_used_bytes": 9876543,
  "created_at": "2025-01-01T00:00:00.000000Z"
}
```

**Repository Info:**
```json
{
  "id": 42,
  "repo_type": "model",
  "namespace": "org",
  "name": "my-model",
  "full_id": "org/my-model",
  "private": false,
  "owner_id": 1,
  "owner_username": "alice",
  "created_at": "2025-01-01T00:00:00.000000Z",
  "file_count": 15,
  "commit_count": 8,
  "total_size": 12345678,
  "quota_bytes": null,
  "used_bytes": 12345678,
  "percentage_used": 0.12,
  "is_inheriting": true
}
```

**Detailed Stats:**
```json
{
  "users": {
    "total": 100,
    "active": 95,
    "verified": 80,
    "inactive": 5
  },
  "organizations": {
    "total": 10
  },
  "repositories": {
    "total": 250,
    "private": 100,
    "public": 150,
    "by_type": {
      "model": 180,
      "dataset": 60,
      "space": 10
    }
  },
  "commits": {
    "total": 1500,
    "top_contributors": [
      {"username": "alice", "commit_count": 150},
      {"username": "bob", "commit_count": 120}
    ]
  },
  "lfs": {
    "total_objects": 500,
    "total_size": 107374182400
  },
  "storage": {
    "private_used": 10737418240,
    "public_used": 53687091200,
    "total_used": 64424509440
  }
}
```

---

## Security Best Practices

### Token Management

**DO:**
- ✅ Generate cryptographically random tokens
- ✅ Use environment variables (never hardcode)
- ✅ Rotate tokens regularly (monthly)
- ✅ Use HTTPS in production
- ✅ Restrict admin portal access (firewall, VPN)

**DON'T:**
- ❌ Use default token in production
- ❌ Commit tokens to git
- ❌ Share tokens via insecure channels
- ❌ Use same token across environments
- ❌ Store tokens in browser localStorage

### Token Rotation

```bash
# 1. Generate new token
NEW_TOKEN=$(openssl rand -hex 32)

# 2. Update docker-compose.yml
KOHAKU_HUB_ADMIN_SECRET_TOKEN: "$NEW_TOKEN"

# 3. Restart services
docker-compose up -d

# 4. Update saved tokens in admin portal sessions
```

### Network Security

**Production Deployment:**

```nginx
# Restrict admin portal to specific IPs
location /admin {
    allow 192.168.1.0/24;  # Internal network
    allow 10.0.0.0/8;      # VPN
    deny all;

    # ... rest of config
}
```

**Alternative: Basic Auth Layer**

```nginx
location /admin/api/ {
    auth_basic "Admin Area";
    auth_basic_user_file /etc/nginx/.htpasswd;

    # Then require X-Admin-Token header
    proxy_pass http://hub-api:48888;
}
```

### Audit Logging

Admin operations are logged with `[ADMIN]` prefix:

```
[WARNING] [ADMIN] [07:05:55] Admin deleted user: testuser (deleted 5 repositories)
[INFO] [ADMIN] [07:06:12] Admin set quota for user alice: private=10737418240, public=53687091200
[WARNING] [ADMIN] [07:06:45] Admin created registration invitation (max_usage=10, expires=30d)
```

**Monitor logs:**
```bash
docker logs khub-hub-api | grep "\[ADMIN\]"
```

---

## Use Cases

### Scenario 1: New User Onboarding

```
1. Dashboard → Quick Actions → "Manage Users"
2. Click "Create User"
3. Fill form:
   - Username: newuser
   - Email: newuser@company.com
   - Password: (generate secure password)
   - Email Verified: ✓
   - Quotas: 10GB private, 50GB public
4. Click "Create User"
5. Share credentials with user
```

### Scenario 2: Invite-Only Registration Mode

```
1. Dashboard → "Manage Invitations"
2. Click "Create Registration Invitation"
3. Configure:
   - Max Usage: 50 (for team)
   - Expires: 90 days
   - Auto-join Organization: my-company (as member)
4. Copy invitation link
5. Share link with team members
6. Monitor usage count
```

### Scenario 3: Storage Cleanup

```
1. Dashboard → "Browse Storage"
2. Click on "hub-storage" bucket
3. Filter by prefix: "lfs/"
4. Review large objects
5. Identify unused LFS objects
6. (Manually delete via CLI/API if needed)
```

### Scenario 4: User Investigation

```
1. Dashboard → "View Commits"
2. Filter by username: "suspicious-user"
3. Review commit activity
4. Click repository links to inspect content
5. If needed: Go to Users → Delete user (with force)
```

### Scenario 5: Quota Enforcement

```
1. Dashboard → "Manage Quotas"
2. Select namespace (user or org)
3. View current usage
4. Set new limits if exceeded
5. Click "Recalculate" to verify
6. Monitor dashboard for compliance
```

### Scenario 6: System Maintenance

```
1. Dashboard → "Bulk Operations"
2. Click "Recalculate All Repository Storage"
3. Optional: Filter by type or namespace
4. Confirm operation
5. Wait for completion (progress logged)
6. Review success/failure report
```

---

## Troubleshooting

### Can't Login

**Problem:** Invalid admin token
**Solution:** Check `KOHAKU_HUB_ADMIN_SECRET_TOKEN` in docker-compose.yml matches your input

---

**Problem:** "Admin API is disabled"
**Solution:** Set `KOHAKU_HUB_ADMIN_ENABLED=true` in environment

---

### Statistics Not Updating

**Problem:** Stale data
**Solution:** Click "Refresh Stats" button on dashboard

---

### Storage Size Incorrect

**Problem:** Database out of sync with S3
**Solution:** Use "Recalculate" button in Quota Management or bulk recalculation endpoint

---

### Can't Delete User

**Problem:** User owns repositories
**Solution:** Either delete repos first, or use "Force Delete" option with `force=true` parameter

---

## Advanced Features

### Time-Series Statistics

**API:**
```bash
curl -H "X-Admin-Token: your-token" \
  "http://localhost:48888/admin/api/stats/timeseries?days=30"
```

**Returns:**
```json
{
  "repositories_by_day": {
    "2025-01-01": {"model": 5, "dataset": 2, "space": 0},
    "2025-01-02": {"model": 3, "dataset": 1, "space": 1}
  },
  "commits_by_day": {
    "2025-01-01": 15,
    "2025-01-02": 20
  },
  "users_by_day": {
    "2025-01-01": 2,
    "2025-01-02": 1
  }
}
```

**Use case:** Build custom dashboards with charts

### Top Repositories

**By Commits:**
```bash
curl -H "X-Admin-Token: your-token" \
  "http://localhost:48888/admin/api/stats/top-repos?by=commits&limit=10"
```

**By Size:**
```bash
curl -H "X-Admin-Token: your-token" \
  "http://localhost:48888/admin/api/stats/top-repos?by=size&limit=10"
```

**Response:**
```json
{
  "top_repositories": [
    {
      "repo_full_id": "org/active-model",
      "repo_type": "model",
      "commit_count": 150,
      "private": false
    }
  ],
  "sorted_by": "commits"
}
```

---

## Integration with CI/CD

### Automated User Creation

```python
import requests

admin_token = "your-admin-token"
base_url = "http://hub.example.com"

# Create user via API
response = requests.post(
    f"{base_url}/admin/api/users",
    headers={"X-Admin-Token": admin_token},
    json={
        "username": "ci-bot",
        "email": "ci@company.com",
        "password": "generated-password",
        "email_verified": True,
        "private_quota_bytes": 107374182400,  # 100 GB
        "public_quota_bytes": None,  # Unlimited
    }
)

user = response.json()
print(f"Created user: {user['username']} (ID: {user['id']})")
```

### Bulk Invitation Generation

```python
import requests

admin_token = "your-admin-token"
base_url = "http://hub.example.com"

# Create reusable invitation for 100 users
response = requests.post(
    f"{base_url}/admin/api/invitations/register",
    headers={"X-Admin-Token": admin_token},
    json={
        "org_id": 5,  # Auto-join org after registration
        "role": "member",
        "max_usage": 100,
        "expires_days": 90
    }
)

invitation = response.json()
print(f"Invitation link: {invitation['invitation_link']}")
print(f"Can be used {invitation['max_usage']} times")
```

### Monitoring Script

```python
import requests

admin_token = "your-admin-token"

# Get statistics
response = requests.get(
    "http://hub.example.com/admin/api/stats/detailed",
    headers={"X-Admin-Token": admin_token}
)

stats = response.json()

# Alert if storage > 80%
total_used = stats['storage']['total_used']
if total_used > 0.8 * (100 * 1000 * 1000 * 1000):  # 80GB
    print("WARNING: Storage usage high!")

# Alert if too many inactive users
if stats['users']['inactive'] > 10:
    print(f"WARNING: {stats['users']['inactive']} inactive users")
```

---

## Performance Considerations

### Database Queries

Admin operations run synchronous queries with `db.atomic()`:
- User listings: `O(n)` where n = total users
- Repository stats: Aggregation queries with indexes
- Commit history: Indexed by repository_id and username
- Storage calculations: Aggregation over File table

**Optimization:**
- Limit page size (default: 100, max: 1000)
- Use filters to reduce result sets
- Statistics are computed on-demand (cache in frontend if needed)

### S3 Bucket Scanning

**Warning:** Scanning large buckets is slow!

```python
# For bucket with 100,000 objects:
# - Scan time: 30-60 seconds
# - Uses pagination (1000 objects per request)
```

**Recommendation:**
- Limit to specific prefixes when possible
- Don't scan too frequently
- Consider caching results for large buckets

### Bulk Storage Recalculation

**Performance:**
- Processes repositories sequentially (safe for database)
- Progress logged every 10 repositories
- Can take 1-5 minutes for 1000 repositories
- Errors don't stop the process (logged and returned)

**Use case:**
- Run during maintenance windows
- Use filters to process subsets
- Monitor logs for progress

---

## Comparison: Admin Portal vs CLI

| Feature | Admin Portal | kohub-cli | Best For |
|---------|--------------|-----------|----------|
| User management | ✅ GUI | ❌ No | Portal: Quick actions |
| Repository browser | ✅ Full | ⚠️ Limited | Portal: Overview<br>CLI: Specific repos |
| Commit history | ✅ Full | ❌ No | Portal only |
| Storage browser | ✅ Full | ❌ No | Portal only |
| Quota management | ✅ Full | ⚠️ API only | Portal: Visual<br>CLI: Scripting |
| Invitation management | ✅ Full | ❌ No | Portal only |
| Statistics | ✅ Dashboard | ❌ No | Portal only |
| Bulk operations | ✅ Full | ❌ No | Portal only |
| Automation | ❌ Manual | ✅ Scripts | Portal: Manual<br>CLI: Automation |

**Recommendation:** Use portal for exploration/monitoring, API for automation.

---

## Frequently Asked Questions

**Q: Can I disable the admin portal?**
A: Yes, set `KOHAKU_HUB_ADMIN_ENABLED=false`

**Q: Is the admin token different from user tokens?**
A: Yes, admin token is system-wide. User tokens are per-user.

**Q: Can I create multiple admin users?**
A: No, admin portal uses shared secret token. For user-based admin, implement role system.

**Q: Does deleting a user delete their repositories?**
A: No (unless force delete). Repositories can be transferred to another user.

**Q: Can I access admin API without the portal UI?**
A: Yes, use curl/Python with `X-Admin-Token` header.

**Q: Is audit logging enabled by default?**
A: Yes, all admin operations are logged with `[ADMIN]` prefix.

**Q: How do I create reusable invitations?**
A: Set `max_usage` to a number (e.g., 50 for 50 uses) or -1 for unlimited.

**Q: Can invitations auto-add users to organizations?**
A: Yes, set `org_id` and `role` in the invitation. Users will automatically join after registration.

---

**Last Updated:** January 2025
**Version:** 1.1
**Status:** ✅ Production Ready
# External Source Fallback System

**Browse repositories from HuggingFace or other KohakuHub instances when not found locally.**

---

## Overview

The fallback system allows KohakuHub to seamlessly access repositories, files, and user profiles from external sources (like HuggingFace.co) when they're not available locally. This enables:

- **Browsing HuggingFace repositories** without manually importing them
- **Downloading files** from external sources
- **Viewing user/org profiles** from other hubs
- **Connecting multiple KohakuHub instances** for federated browsing

---

## Quick Start

### 1. Configure Fallback Source (Admin Portal)

Navigate to: **Admin Portal → Fallback Sources**

**Add HuggingFace:**
```
Name: HuggingFace
URL: https://huggingface.co
Source Type: huggingface
Priority: 1
Token: (optional - for private repos)
Namespace: (empty for global)
Enabled: ✓
```

### 2. Browse External Repositories

**Visit any HuggingFace user/org:**
```
http://localhost:28080/openai
http://localhost:28080/stabilityai
```

**View external models/datasets:**
```
http://localhost:28080/models/openai/whisper-tiny
http://localhost:28080/datasets/karpathy/fineweb-edu
```

**Download files:**
```python
from huggingface_hub import hf_hub_download

# Falls back to HuggingFace automatically
hf_hub_download(
    repo_id="openai/whisper-tiny",
    filename="model.bin"
)
```

---

## How It Works

### Architecture

```
User Request → KohakuHub
  ↓
  Check Local Database
  ↓
  Not Found (404)
  ↓
  Try Fallback Sources (by priority)
    1. HuggingFace
    2. Other KohakuHub Instance
    ...
  ↓
  Found! → Return with _source tag
```

### Caching

**Repository→Source mapping is cached** (not content):
- **Cache TTL**: 5 minutes (configurable)
- **Cache Key**: `{repo_type}:{namespace}/{name}`
- **Cache Value**: Source URL, name, type

This reduces external API calls by 80%+.

---

## Configuration

### Global Sources (Environment Variable)

**In `docker-compose.yml`:**
```yaml
environment:
  KOHAKU_HUB_FALLBACK_ENABLED: "true"
  KOHAKU_HUB_FALLBACK_CACHE_TTL: "300"  # 5 minutes
  KOHAKU_HUB_FALLBACK_TIMEOUT: "10"     # 10 seconds
  KOHAKU_HUB_FALLBACK_SOURCES: |
    [
      {
        "url": "https://huggingface.co",
        "token": "",
        "priority": 1,
        "name": "HuggingFace",
        "source_type": "huggingface"
      }
    ]
```

### Database Sources (via Admin API)

**Add via API:**
```bash
curl -X POST http://localhost:48888/admin/api/fallback-sources \
  -H "X-Admin-Token: your-admin-token" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "",
    "url": "https://huggingface.co",
    "name": "HuggingFace",
    "source_type": "huggingface",
    "priority": 1,
    "enabled": true
  }'
```

**Or via Admin Portal:**
- Navigate to: http://localhost:28080/admin/fallback-sources
- Click "Add Source"
- Fill in details

---

## Supported Operations

### Repository Operations

✅ **Resolve/Download Files**
- `GET /{type}s/{namespace}/{name}/resolve/{revision}/{path}`
- Returns 302 redirect to external URL

✅ **List Files (Tree)**
- `GET /api/{type}s/{namespace}/{name}/tree/{revision}`
- Returns file tree from external source

✅ **Repository Info**
- `GET /api/{type}s/{namespace}/{name}`
- Returns metadata from external source

✅ **Revision Info**
- `GET /api/{type}s/{namespace}/{name}/revision/{revision}`
- Returns commit/branch info

### User/Organization Operations

✅ **User Profile**
- `GET /api/users/{username}/profile`
- Falls back to HF `/api/users/{username}/overview`

✅ **User Repositories**
- `GET /api/users/{username}/repos`
- Aggregates from `/api/models`, `/api/datasets`, `/api/spaces`

✅ **Organization Profile**
- Detected via `/api/organizations/{name}/members`
- Shows as organization page

### List Aggregation

✅ **Repository Lists**
- `GET /api/models?author={name}`
- Merges local + external results with `_source` tags

❌ **Disabled by Default On:**
- Homepage trending lists (`?fallback=false`)
- Main browse pages (`/models`, `/datasets`, `/spaces`)

---

## URL Mapping (HuggingFace)

**HuggingFace has asymmetric URL patterns:**

| Operation | KohakuHub | HuggingFace |
|-----------|-----------|-------------|
| **Models Download** | `/models/{ns}/{name}/resolve/...` | `/{ns}/{name}/resolve/...` |
| **Datasets Download** | `/datasets/{ns}/{name}/resolve/...` | `/datasets/{ns}/{name}/resolve/...` |
| **API Endpoints** | `/api/{type}s/...` | `/api/{type}s/...` |

The fallback client automatically handles these transformations.

---

## External Source Indicators

### Repository Pages

**External repos show:**
- Badge in header: `[☁️ External: https://huggingface.co]`
- Disabled commits tab (not available for external repos)
- All metadata tagged with `_source` field

### User/Org Pages

**External profiles show:**
- Badge in profile card: `[☁️ HuggingFace]`
- "Limited profile" indicator (bio/website may be missing)
- All repos tagged with source

---

## Query Parameters

**Disable fallback per-request:**
```
GET /api/models?fallback=false
```

Useful for:
- Homepage (show local only)
- Admin interfaces
- Performance-critical lists

---

## Admin Interface

**Fallback Sources Management:**

**Access:** http://localhost:28080/admin/fallback-sources

**Features:**
- Add/Edit/Delete sources
- Enable/Disable sources
- Set priority order
- View cache statistics
- Clear cache manually

**Cache Stats:**
- Current size
- Max size (10,000 entries)
- TTL (300 seconds default)
- Usage percentage

---

## Limitations

### What Works

✅ Browsing external repos (tree, files, metadata)
✅ Downloading files (302 redirect to external)
✅ Viewing user/org profiles
✅ Listing user's repositories
✅ YAML frontmatter metadata

### What Doesn't Work

❌ **Commits** - Not available for external repos
❌ **Editing** - Can't modify external repos
❌ **Git Clone** - Only local repos support Git clone
❌ **LFS Upload** - Can't upload to external sources
❌ **Private Access** - Requires admin-configured tokens (no user token passthrough)

---

## Security

**User Privacy:**
- ❌ Local user credentials are **NEVER** sent to external sources
- ✅ Only admin-configured tokens are used
- ✅ Public repos work without any tokens

**Admin Token:**
- Configure once in admin portal
- Used for all external requests
- Can access private repos on external source (if token has permission)

---

## Troubleshooting

**External repos not showing:**
1. Check fallback sources in admin portal
2. Verify source is enabled
3. Check cache TTL (may need to wait or clear cache)
4. Look for errors in backend logs

**404 errors for external content:**
1. Verify the repo exists on the external source
2. Check if source URL is correct
3. Try clearing cache in admin portal

**Performance issues:**
1. Check cache stats (should be >80% hit rate)
2. Reduce number of external sources
3. Increase cache TTL
4. Use `?fallback=false` for performance-critical pages

---

## Advanced Configuration

### Multiple Sources

**Priority ordering:**
```json
[
  {"url": "https://your-hub.com", "priority": 1, "name": "Internal"},
  {"url": "https://huggingface.co", "priority": 2, "name": "HuggingFace"}
]
```

Lower priority = checked first.

### Per-Namespace Sources

**User/org-specific fallback:**
```json
{
  "namespace": "my-team",
  "url": "https://team-hub.com",
  "priority": 1
}
```

Only applies when browsing `my-team/*` repos.

### Cache Tuning

```bash
KOHAKU_HUB_FALLBACK_CACHE_TTL=600       # 10 minutes
KOHAKU_HUB_FALLBACK_TIMEOUT=20          # 20 second timeout
KOHAKU_HUB_FALLBACK_MAX_CONCURRENT=10   # 10 concurrent requests
```

---

## API Reference

**Admin Endpoints:**
```
POST   /admin/api/fallback-sources           # Create source
GET    /admin/api/fallback-sources           # List sources
GET    /admin/api/fallback-sources/{id}      # Get source
PUT    /admin/api/fallback-sources/{id}      # Update source
DELETE /admin/api/fallback-sources/{id}      # Delete source
GET    /admin/api/fallback-sources/cache/stats    # Cache stats
DELETE /admin/api/fallback-sources/cache/clear    # Clear cache
```

**Query Parameters:**
```
?fallback=false    # Disable fallback for this request
?fallback=true     # Enable fallback (default)
```

---

## Examples

### Example 1: Browse HuggingFace Models

```bash
# View Stability AI's models
curl http://localhost:28080/api/models?author=stabilityai

# Returns local + HuggingFace models tagged with _source
```

### Example 2: Download from HuggingFace

```python
from huggingface_hub import hf_hub_download

# Falls back to HuggingFace automatically
model_path = hf_hub_download(
    repo_id="openai/whisper-tiny",
    filename="config.json"
)
```

### Example 3: Federated KohakuHub

**Connect company internal hub:**
```json
{
  "url": "https://internal-hub.company.com",
  "source_type": "kohakuhub",
  "priority": 1,
  "token": "internal_token_here"
}
```

Now you can browse internal repos + HuggingFace from one interface!

---

## Performance

**Typical Response Times:**
- **Cache Hit**: <100ms (instant)
- **Cache Miss (HF)**: <2s (external API call)
- **File Download**: 302 redirect (no proxy, full speed)

**Cache Hit Rate:**
- **Expected**: >80% after warmup
- **Check**: Admin Portal → Fallback Sources → Cache Stats

---

## See Also

- [Admin Portal Guide](./Admin.md#fallback-sources-management)
- [API Documentation](./API.md)
- [Deployment Guide](./deployment.md)
