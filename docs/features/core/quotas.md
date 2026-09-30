---
title: Quota Management
description: Storage limits for users, organizations, and repositories
icon: i-carbon-meter
---

# Quota Management

Storage limits for users, organizations, and repositories.

---

## Overview

**Separate quotas for:**
- Public repositories
- Private repositories
- Per-user defaults
- Per-organization defaults
- Per-repository overrides

**Inheritance:**
- Repository quota → Namespace quota → Server default
- NULL = unlimited (or inherit from parent)

---

## Configuration

### Server Defaults

```yaml
# Environment variables
KOHAKU_HUB_DEFAULT_USER_PUBLIC_QUOTA_BYTES: null     # Unlimited
KOHAKU_HUB_DEFAULT_USER_PRIVATE_QUOTA_BYTES: null    # Unlimited
KOHAKU_HUB_DEFAULT_ORG_PUBLIC_QUOTA_BYTES: null      # Unlimited
KOHAKU_HUB_DEFAULT_ORG_PRIVATE_QUOTA_BYTES: null     # Unlimited
```

**Example with limits:**

```yaml
KOHAKU_HUB_DEFAULT_USER_PUBLIC_QUOTA_BYTES: 10737418240   # 10GB
KOHAKU_HUB_DEFAULT_USER_PRIVATE_QUOTA_BYTES: 5368709120   # 5GB
KOHAKU_HUB_DEFAULT_ORG_PUBLIC_QUOTA_BYTES: 107374182400   # 100GB
KOHAKU_HUB_DEFAULT_ORG_PRIVATE_QUOTA_BYTES: 53687091200   # 50GB
```

### Per-User Quotas

**Admin Portal:**
1. Users → Select user → Edit
2. Set custom quotas:
   - Public quota (bytes or null)
   - Private quota (bytes or null)
3. Save

**API:**

```bash
# Get user quota
curl http://localhost:28080/api/quota/username

# Response
{
  "public_quota_bytes": 10737418240,
  "private_quota_bytes": 5368709120,
  "public_used_bytes": 1234567,
  "private_used_bytes": 7654321,
  "public_percentage_used": 0.011,
  "private_percentage_used": 0.142
}
```

### Per-Repository Quotas

**Repository-specific limits:**

```bash
# Via API (admin only)
curl -X PUT http://localhost:28080/api/models/username/repo/settings \\
  -d '{"quota_bytes": 1073741824}'  # 1GB limit for this repo
```

**Inheritance:**
- If repo quota is NULL → Inherits from namespace
- Namespace checks both repo and namespace limits

---

## Checking Quotas

### User Quota

**Web UI:**
- Click username → Storage
- See public/private usage
- Progress bars and percentages

**API:**

```bash
# Public quota (anyone can view)
curl http://localhost:28080/api/quota/username/public

# Full quota (authenticated users)
curl http://localhost:28080/api/quota/username \\
  -H "Authorization: Bearer token"
```

### Repository Quota

**Web UI:**
- Repo page → Sidebar → Storage card
- Shows: used / limit

**API:**

```bash
curl http://localhost:28080/api/quota/repo/model/username/repo
```

---

## Quota Enforcement

**Upload blocked when:**
- Total upload size + current usage > quota
- Checked at preupload stage
- Error 413: Payload Too Large

**How usage is counted:**
- A repository uses its regular files on `main`, plus every LFS object any
  branch's history links that is still stored, each counted once (by SHA256)
- Deleting an LFS file frees nothing until garbage collection removes the
  object; then every repository that linked it stops counting it
- A user's or organization's usage is the sum over its repositories,
  private and public apart

Usage is kept up to date as repositories change: every commit, branch
operation (merge, revert, reset) and garbage collection applies its
difference, in proportion to what it changed. Nothing lists a repository on
the way. A change the counters cannot follow (for example `main` moved
outside KohakuHub) recounts that repository in the background.

---

## Admin Tools

### Quota Overview

**Admin Portal → Quota Overview:**
- All users and quotas at a glance
- Sort by usage, percentage
- Filter by over-limit

### Recount Usage

A recount sets exact usage from what repositories hold, as the
`usage.recount` background task. It reports how far the kept numbers had
drifted, the largest drifts first. It runs once on its own after upgrading
to a version with kept usage (migration 023). Start it from **Admin Portal →
Storage → Storage usage recount**, which also shows the latest report, or:

```bash
# Every repository
curl -X POST http://localhost:28080/admin/api/usage/recount -H "X-Admin-Token: admin_token"
curl http://localhost:28080/admin/api/usage/recount -H "X-Admin-Token: admin_token"

# One user's or organization's repositories (background)
curl -X POST "http://localhost:28080/admin/api/quota/username/recalculate" -H "X-Admin-Token: admin_token"

# One repository, at once (its write access suffices)
curl -X POST http://localhost:28080/api/quota/repo/model/username/repo/recalculate
```

The recount runs on a background worker (`khub-worker`); the card warns
when none is online, since recounts then wait in the queue.

`KOHAKU_HUB_USAGE_RECOUNT_INTERVAL_HOURS` repeats the site-wide recount
periodically as a safety net (off by default).

### View Storage Breakdown

**Admin Portal → Repositories:**
- Storage breakdown per repo
- Public vs private usage
- File count
- LFS object count

---

## Best Practices

**For admins:**
- Start with unlimited quotas
- Monitor actual usage
- Set limits based on data
- Communicate limits to users

**For users:**
- Check quota before large uploads
- Use efficient file formats
- Clean up old versions
- Archive unused repos

**Quota sizing:**
- 10GB per user (small team)
- 50-100GB per organization
- Per-repo limits for critical ones

---

## Troubleshooting

**Upload rejected (413):**
- Check quota: `/api/quota/username/public`
- Contact admin for increase
- Delete unused files
- Use organization quota instead

**Quota shows wrong number:**
- Recount: Admin Portal → Storage → Storage usage recount; its report lists
  the repositories that had drifted

**Over quota but can't find files:**
- Old LFS versions
- Run garbage collection
- Check hidden/deleted files

---

See also: [LFS Configuration](./lfs.md), [Admin Portal](../api/admin.md)
