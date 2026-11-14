---
title: Invitation API
description: Invite users to organizations with flexible invitation types
icon: mail
---

# Invitation API

Create and manage invitations to join organizations. Supports both single-use and reusable invitations.

Repository invitation codes use a separate, hashed repository-specific lifecycle. They do not
join an organization or act as HF access tokens. See [Invitation repositories](../features/invitation-repositories.md)
for application review, registration/account binding, temporary guests and the browser API paths.

## Overview

KohakuHub invitation system supports:

- **Email-specific invitations**: Sent to a specific email address
- **Reusable invitations**: Can be used multiple times (link sharing)
- **Unlimited invitations**: No usage limit (perfect for public organizations)
- **Expiration control**: Set custom expiration (1-365 days)
- **Role assignment**: Specify member role when creating invitation

---

## Endpoints

### Create Organization Invitation

Create an invitation for users to join an organization.

**Endpoint:** `POST /api/invitations/org/{org_name}/create`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `org_name` | string | path | Yes | Organization name |

**Request Body:**

```json
{
  "email": "alice@example.com",
  "role": "member",
  "max_usage": null,
  "expires_days": 7
}
```

| Field | Type | Required | Description | Default |
|-------|------|----------|-------------|---------|
| `email` | string (email) | No | Recipient email (null for reusable invites) | null |
| `role` | string | Yes | Member role: `visitor`, `member`, or `admin` | - |
| `max_usage` | integer | No | Max uses: `null` (single-use), `-1` (unlimited), or positive number | null |
| `expires_days` | integer | Yes | Days until expiration (1-365) | 7 |

**Authentication:** Required (admin or super-admin)

**Response:**

```json
{
  "success": true,
  "token": "abc123xyz789...",
  "invitation_link": "http://localhost:28080/invite/abc123xyz789...",
  "expires_at": "2025-01-22T10:30:00Z",
  "max_usage": null,
  "is_reusable": false
}
```

**Invitation Types:**

1. **Single-use (default)**: `max_usage: null` - Can be used once, then expired
2. **Limited reusable**: `max_usage: 5` - Can be used up to 5 times
3. **Unlimited**: `max_usage: -1` - Can be used unlimited times (until expiration)

**Email Behavior:**

- If `email` is provided and SMTP is configured, invitation email is sent automatically
- If `email` is null, invitation link can be shared manually (for reusable invites)
- Email-specific invitations check if user is already a member before creating

**Error Responses:**

| Status | Description |
|--------|-------------|
| 400 | Invalid role, user already a member, or invalid parameters |
| 403 | Not authorized to invite members |
| 404 | Organization not found |

**Example:**

```python
import requests

BASE_URL = "http://localhost:28080"
TOKEN = "YOUR_TOKEN"

headers = {"Authorization": f"Bearer {TOKEN}"}

# Single-use email invitation
response = requests.post(
    f"{BASE_URL}/api/invitations/org/myorg/create",
    headers=headers,
    json={
        "email": "alice@example.com",
        "role": "member",
        "expires_days": 7
    }
)
invite = response.json()
print(f"Invitation sent to alice@example.com")

# Reusable link (5 uses)
response = requests.post(
    f"{BASE_URL}/api/invitations/org/myorg/create",
    headers=headers,
    json={
        "role": "member",
        "max_usage": 5,
        "expires_days": 30
    }
)
invite = response.json()
print(f"Share this link: {invite['invitation_link']}")

# Unlimited public invite
response = requests.post(
    f"{BASE_URL}/api/invitations/org/myorg/create",
    headers=headers,
    json={
        "role": "visitor",
        "max_usage": -1,
        "expires_days": 365
    }
)
invite = response.json()
print(f"Public invite link: {invite['invitation_link']}")
```

---

### Get Invitation Details

Get invitation information without accepting it.

**Endpoint:** `GET /api/invitations/{token}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `token` | string | path | Yes | Invitation token |

**Authentication:** Optional

**Response:**

```json
{
  "action": "join_org",
  "org_name": "myorg",
  "role": "member",
  "email": "alice@example.com",
  "inviter_username": "admin",
  "expires_at": "2025-01-22T10:30:00Z",
  "is_expired": false,
  "is_available": true,
  "error_message": null,
  "max_usage": null,
  "usage_count": 0,
  "is_reusable": false
}
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `action` | string | Invitation action type (`join_org` or `register_account`) |
| `org_name` | string | Organization name |
| `role` | string | Assigned role |
| `email` | string | Target email (null for reusable invites) |
| `inviter_username` | string | User who created the invitation |
| `expires_at` | string | ISO 8601 expiration timestamp |
| `is_expired` | boolean | Whether invitation has expired |
| `is_available` | boolean | Whether invitation can be used |
| `error_message` | string | Reason if not available (null if available) |
| `max_usage` | integer | Maximum uses (null = single-use, -1 = unlimited) |
| `usage_count` | integer | Number of times used |
| `is_reusable` | boolean | Whether invitation can be used multiple times |

**Availability Conditions:**

An invitation is available if ALL of the following are true:
- Not expired (`expires_at` > now)
- Usage count < max_usage (or max_usage is -1 for unlimited)
- For single-use invitations: not yet used

**Example:**

```python
response = requests.get(
    f"{BASE_URL}/api/invitations/{token}"
)
invite = response.json()

if invite["is_available"]:
    print(f"Valid invite to join {invite['org_name']} as {invite['role']}")
else:
    print(f"Invite not available: {invite['error_message']}")

if invite["is_reusable"]:
    print(f"Used {invite['usage_count']} / {invite['max_usage']} times")
```

---

### Accept Invitation

Accept an invitation and join the organization.

**Endpoint:** `POST /api/invitations/{token}/accept`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `token` | string | path | Yes | Invitation token |

**Authentication:** Required

**Response:**

```json
{
  "success": true,
  "message": "You have successfully joined myorg as a member",
  "org_name": "myorg",
  "role": "member"
}
```

**Behavior:**

- Adds authenticated user to organization with specified role
- Increments usage count for reusable invitations
- Marks single-use invitations as used
- Checks if user is already a member (returns error if so)

**Error Responses:**

| Status | Description |
|--------|-------------|
| 400 | Invitation expired, max usage reached, or already a member |
| 401 | Authentication required |
| 404 | Invitation not found |

**Example:**

```python
# User accepts invitation
response = requests.post(
    f"{BASE_URL}/api/invitations/{token}/accept",
    headers={"Authorization": f"Bearer {USER_TOKEN}"}
)

result = response.json()
print(result["message"])
```

---

### List Organization Invitations

List all invitations for an organization (admin only).

**Endpoint:** `GET /api/invitations/org/{org_name}/list`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `org_name` | string | path | Yes | Organization name |

**Authentication:** Required (admin or super-admin)

**Response:**

```json
{
  "invitations": [
    {
      "id": 1,
      "token": "abc123...",
      "email": "alice@example.com",
      "role": "member",
      "created_by": "admin",
      "created_at": "2025-01-15T10:30:00Z",
      "expires_at": "2025-01-22T10:30:00Z",
      "max_usage": null,
      "usage_count": 0,
      "is_reusable": false,
      "is_available": true,
      "used_at": null,
      "is_pending": true
    },
    {
      "id": 2,
      "token": "xyz789...",
      "email": null,
      "role": "visitor",
      "created_by": "admin",
      "created_at": "2025-01-10T10:30:00Z",
      "expires_at": "2026-01-10T10:30:00Z",
      "max_usage": -1,
      "usage_count": 42,
      "is_reusable": true,
      "is_available": true,
      "used_at": null,
      "is_pending": false
    }
  ]
}
```

**Field Descriptions:**

| Field | Description |
|-------|-------------|
| `is_pending` | True if available and never used (single-use invites only) |
| `is_available` | Whether invitation can still be used |
| `used_at` | Timestamp when first used (for single-use invites) |

**Example:**

```python
response = requests.get(
    f"{BASE_URL}/api/invitations/org/myorg/list",
    headers={"Authorization": f"Bearer {TOKEN}"}
)

invitations = response.json()["invitations"]

# Filter by status
pending = [i for i in invitations if i["is_pending"]]
active_reusable = [i for i in invitations if i["is_reusable"] and i["is_available"]]
expired = [i for i in invitations if not i["is_available"]]

print(f"Pending: {len(pending)}")
print(f"Active reusable: {len(active_reusable)}")
print(f"Expired: {len(expired)}")
```

---

### Delete Invitation

Cancel/delete an invitation (admin only).

**Endpoint:** `DELETE /api/invitations/{token}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `token` | string | path | Yes | Invitation token |

**Authentication:** Required (admin or super-admin)

**Response:**

```json
{
  "success": true,
  "message": "Invitation deleted successfully"
}
```

**Use Cases:**

- Cancel pending invitations
- Remove expired invitations
- Revoke reusable invitation links

**Error Responses:**

| Status | Description |
|--------|-------------|
| 403 | Not authorized to delete this invitation |
| 404 | Invitation not found |

**Example:**

```python
response = requests.delete(
    f"{BASE_URL}/api/invitations/{token}",
    headers={"Authorization": f"Bearer {TOKEN}"}
)
print("Invitation cancelled")
```

---

## Usage Examples

### Managing Invitations Workflow

```python
import requests
from datetime import datetime

BASE_URL = "http://localhost:28080"
TOKEN = "YOUR_TOKEN"

headers = {"Authorization": f"Bearer {TOKEN}"}

class InvitationManager:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}

    def create_invite(self, org_name: str, email: str = None, role: str = "member",
                     max_usage: int = None, expires_days: int = 7):
        """Create invitation."""
        response = requests.post(
            f"{self.base_url}/api/invitations/org/{org_name}/create",
            headers=self.headers,
            json={
                "email": email,
                "role": role,
                "max_usage": max_usage,
                "expires_days": expires_days
            }
        )
        response.raise_for_status()
        return response.json()

    def list_invites(self, org_name: str):
        """List all organization invitations."""
        response = requests.get(
            f"{self.base_url}/api/invitations/org/{org_name}/list",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()["invitations"]

    def get_invite_details(self, token: str):
        """Get invitation details."""
        response = requests.get(
            f"{self.base_url}/api/invitations/{token}"
        )
        response.raise_for_status()
        return response.json()

    def cancel_invite(self, token: str):
        """Cancel/delete invitation."""
        response = requests.delete(
            f"{self.base_url}/api/invitations/{token}",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def cleanup_expired(self, org_name: str):
        """Delete all expired invitations."""
        invites = self.list_invites(org_name)
        expired = [i for i in invites if not i["is_available"]]

        for invite in expired:
            self.cancel_invite(invite["token"])
            print(f"Deleted expired invite: {invite['token'][:8]}...")

        return len(expired)

# Usage
mgr = InvitationManager(BASE_URL, TOKEN)

# Create different types of invitations
email_invite = mgr.create_invite("myorg", email="alice@example.com", role="member")
reusable_invite = mgr.create_invite("myorg", role="member", max_usage=10, expires_days=30)
public_invite = mgr.create_invite("myorg", role="visitor", max_usage=-1, expires_days=365)

print(f"Email invite: {email_invite['invitation_link']}")
print(f"Reusable invite (10 uses): {reusable_invite['invitation_link']}")
print(f"Public invite (unlimited): {public_invite['invitation_link']}")

# List and cleanup
invites = mgr.list_invites("myorg")
print(f"Total invitations: {len(invites)}")

expired_count = mgr.cleanup_expired("myorg")
print(f"Cleaned up {expired_count} expired invitations")
```

### Invitation Statistics

```python
def get_invitation_stats(org_name: str, token: str):
    """Get comprehensive invitation statistics."""
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        f"{BASE_URL}/api/invitations/org/{org_name}/list",
        headers=headers
    )
    invites = response.json()["invitations"]

    stats = {
        "total": len(invites),
        "pending": 0,
        "used": 0,
        "expired": 0,
        "reusable_active": 0,
        "total_uses": 0,
        "by_role": {"visitor": 0, "member": 0, "admin": 0}
    }

    for invite in invites:
        # Count by status
        if invite["is_pending"]:
            stats["pending"] += 1
        elif not invite["is_available"]:
            stats["expired"] += 1
        elif invite["is_reusable"]:
            stats["reusable_active"] += 1

        if invite["usage_count"] > 0:
            stats["used"] += 1
            stats["total_uses"] += invite["usage_count"]

        # Count by role
        stats["by_role"][invite["role"]] += 1

    return stats

# Usage
stats = get_invitation_stats("myorg", TOKEN)
print(f"Total invitations: {stats['total']}")
print(f"  Pending: {stats['pending']}")
print(f"  Used: {stats['used']} ({stats['total_uses']} total uses)")
print(f"  Expired: {stats['expired']}")
print(f"  Active reusable: {stats['reusable_active']}")
print(f"\nBy role:")
for role, count in stats["by_role"].items():
    print(f"  {role}: {count}")
```

### User Accepting Invitation

```python
def accept_invite_flow(token: str, user_token: str):
    """Complete invitation acceptance flow."""

    # 1. Check invitation details (no auth needed)
    response = requests.get(f"{BASE_URL}/api/invitations/{token}")
    invite = response.json()

    if not invite["is_available"]:
        print(f"Error: {invite['error_message']}")
        return False

    print(f"You are invited to join {invite['org_name']} as {invite['role']}")
    print(f"Invited by: {invite['inviter_username']}")
    print(f"Expires: {invite['expires_at']}")

    if invite["is_reusable"]:
        print(f"This is a reusable link (used {invite['usage_count']} times)")

    # 2. Accept invitation (requires authentication)
    response = requests.post(
        f"{BASE_URL}/api/invitations/{token}/accept",
        headers={"Authorization": f"Bearer {user_token}"}
    )

    if response.status_code == 200:
        result = response.json()
        print(f"\nSuccess! {result['message']}")
        return True
    else:
        error = response.json()
        print(f"\nFailed: {error.get('detail', 'Unknown error')}")
        return False

# Usage
success = accept_invite_flow("abc123xyz789...", "USER_TOKEN")
```

---

## JavaScript/TypeScript Example

```javascript
class InvitationAPI {
  constructor(baseURL, token) {
    this.baseURL = baseURL;
    this.headers = {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    };
  }

  async createInvite(orgName, options = {}) {
    const {
      email = null,
      role = 'member',
      maxUsage = null,
      expiresDays = 7
    } = options;

    const response = await fetch(
      `${this.baseURL}/api/invitations/org/${orgName}/create`,
      {
        method: 'POST',
        headers: this.headers,
        body: JSON.stringify({
          email,
          role,
          max_usage: maxUsage,
          expires_days: expiresDays
        })
      }
    );
    return await response.json();
  }

  async listInvites(orgName) {
    const response = await fetch(
      `${this.baseURL}/api/invitations/org/${orgName}/list`,
      { headers: this.headers }
    );
    const data = await response.json();
    return data.invitations;
  }

  async getInviteDetails(token) {
    const response = await fetch(
      `${this.baseURL}/api/invitations/${token}`
    );
    return await response.json();
  }

  async acceptInvite(token) {
    const response = await fetch(
      `${this.baseURL}/api/invitations/${token}/accept`,
      {
        method: 'POST',
        headers: this.headers
      }
    );
    return await response.json();
  }

  async cancelInvite(token) {
    const response = await fetch(
      `${this.baseURL}/api/invitations/${token}`,
      {
        method: 'DELETE',
        headers: this.headers
      }
    );
    return await response.json();
  }
}

// Usage
const inviteAPI = new InvitationAPI('http://localhost:28080', 'YOUR_TOKEN');

// Create invitations
const emailInvite = await inviteAPI.createInvite('myorg', {
  email: 'alice@example.com',
  role: 'member'
});

const reusableInvite = await inviteAPI.createInvite('myorg', {
  role: 'member',
  maxUsage: 10,
  expiresDays: 30
});

console.log('Share this link:', reusableInvite.invitation_link);

// List and manage
const invites = await inviteAPI.listInvites('myorg');
const pending = invites.filter(i => i.is_pending);
console.log(`${pending.length} pending invitations`);
```

---

## CLI Usage

See [CLI Documentation](../CLI.md#invitations) for command-line interface:

```bash
# Create email invitation
kohub-cli invite create myorg alice@example.com --role member --expires 7

# Create reusable invitation
kohub-cli invite create myorg --role member --max-usage 10 --expires 30

# List invitations
kohub-cli invite list myorg

# Cancel invitation
kohub-cli invite cancel TOKEN

# Accept invitation (as user)
kohub-cli invite accept TOKEN
```

---

## Next Steps

- [Organization Management API](./organizations.md) - Manage organizations and members
- [Settings API](./settings.md) - Configure organization settings
- [Authentication](../API.md#authentication) - Token-based authentication
