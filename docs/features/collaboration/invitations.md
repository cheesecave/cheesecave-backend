---
title: Invitations
description: Invitation-only registration and auto-org-join
icon: i-carbon-email
---

# Invitation System

Control who can register and auto-add to organizations.

## Enable Invitation-Only Mode

```yaml
CHEESE_CAVE_INVITATION_ONLY: true
```

## Create Invitations

**Admin Portal → Invitations:**

**Types:**
- Single-use (max_usage: 1)
- Multi-use (max_usage: 50)
- Unlimited (max_usage: -1)

**Auto-org-join:**
- Set org_id and role
- User auto-joins on registration

## API

```bash
POST /admin/api/invitations/register
{
  "max_usage": 10,
  "expires_days": 30,
  "org_id": 5,
  "role": "member"
}
```
