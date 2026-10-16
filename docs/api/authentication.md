---
title: Authentication API
description: User registration, login, tokens, email verification, sessions
icon: i-carbon-password
---

# Authentication API

User authentication, registration, API token management, and email verification.

---

## Registration

### Register New User

**Pattern:** `POST /auth/register`

**Authentication:** Public (no auth required)

**Query Parameters:**
- `invitation_token` (optional): Required if server is in invitation-only mode

**Request Body:**
```json
{
  "username": "alice",
  "email": "alice@example.com",
  "password": "secure_password_123"
}
```

**Response:**
```json
{
  "success": true,
  "message": "User created successfully",
  "email_verified": true
}
```

**With Email Verification Enabled:**
```json
{
  "success": true,
  "message": "User created. Please check your email to verify your account.",
  "email_verified": false
}
```

**Validation:**
- Username: Not reserved, not already taken, no normalized conflicts
- Email: Valid format, not already registered
- Password: Minimum length (enforced by server config)

**Reserved Usernames:**
`models`, `datasets`, `spaces`, `admin`, `api`, `organizations`, `settings`, `new`, `login`, `register`, `logout`, `docs`, and more (see validation.py)

**Status Codes:**
- `200 OK` - User created
- `400 Bad Request` - Username/email taken, reserved name, or validation error
- `403 Forbidden` - Invitation required but not provided

---

## Login & Sessions

### Login

**Pattern:** `POST /auth/login`

**Authentication:** Public

**Request Body:**
```json
{
  "username": "alice",
  "password": "secure_password_123"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Logged in successfully",
  "username": "alice",
  "session_secret": "random_secret_for_client_side_encryption"
}
```

**Response Headers:**
```
Set-Cookie: session_id=abc123...; HttpOnly; SameSite=Lax; Max-Age=2592000
```

**Session Duration:** 30 days (default, configurable)

**Checks:**
- Username and password match
- Account is active (`is_active = true`)
- Email verified (if `require_email_verification = true`)

**Status Codes:**
- `200 OK` - Logged in successfully
- `401 Unauthorized` - Invalid credentials
- `403 Forbidden` - Account disabled or email not verified

---

### Logout

**Pattern:** `POST /auth/logout`

**Authentication:** Required (session cookie or token)

**Response:**
```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

**Behavior:**
- Deletes all user sessions from database
- Clears session_id cookie

---

### Get Current User

**Pattern:** `GET /auth/me`

**Authentication:** Required

**Response:**
```json
{
  "id": 1,
  "username": "alice",
  "email": "alice@example.com",
  "email_verified": true,
  "created_at": "2025-01-01T00:00:00Z"
}
```

**Use Case:** Check if still logged in, get basic user info

---

## API Tokens

### List Tokens

**Pattern:** `GET /auth/tokens`

**Authentication:** Required (session cookie or existing token)

**Response:**
```json
{
  "tokens": [
    {
      "id": 1,
      "name": "My API Token",
      "last_used": "2025-01-20T10:15:32Z",
      "created_at": "2025-01-01T00:00:00Z"
    },
    {
      "id": 2,
      "name": "CI/CD Token",
      "last_used": null,
      "created_at": "2025-01-15T08:00:00Z"
    }
  ]
}
```

**Notes:**
- Token values are never shown after creation
- `last_used` updates when token is used
- Tokens don't expire by default

---

### Create Token

**Pattern:** `POST /auth/tokens/create`

**Authentication:** Required

**Request Body:**
```json
{
  "name": "My API Token"
}
```

**Response:**
```json
{
  "success": true,
  "token": "hf_abc123def456ghi789jkl...",
  "token_id": 3,
  "session_secret": "encryption_secret",
  "message": "Token created. Save it securely - you won't see it again!"
}
```

**Important:**
- Token value shown **only once**
- Copy immediately and store securely
- Cannot retrieve token value later
- Prefix: `hf_` (HuggingFace compatible)

---

### Revoke Token

**Pattern:** `DELETE /auth/tokens/{token_id}`

**Authentication:** Required (must own token)

**Response:**
```json
{
  "success": true,
  "message": "Token revoked successfully"
}
```

**Status Codes:**
- `200 OK` - Token revoked
- `404 Not Found` - Token not found or not owned by user

---

## Email Verification

### Verify Email

**Pattern:** `GET /auth/verify-email?token={verification_token}`

**Authentication:** Public

**Purpose:** Verify email address after registration

**Response:**
- Redirects to user profile on success: `302 -> /{username}`
- Redirects to login with error on failure: `302 -> /?error=invalid_token`

**Behavior:**
1. Validates verification token
2. Checks token not expired (24 hours default)
3. Marks user email as verified
4. Deletes verification token
5. Creates session and auto-logs in user

**Cookie Set:**
```
Set-Cookie: session_id=abc123...; HttpOnly; SameSite=Lax
```

---

### Resend Verification Email

**Pattern:** `POST /auth/resend-verification`

**Authentication:** Required (can be unverified user)

**Purpose:** Resend verification email if not received

**Response:**
```json
{
  "success": true,
  "message": "Verification email sent"
}
```

**Validation:**
- User must not be already verified
- Rate limit: Once per hour (TODO: implement)

---

## Usage Examples

### Register and Login

```python
import requests

API_BASE = "http://localhost:28080"

# Register
register_resp = requests.post(
    f"{API_BASE}/auth/register",
    json={
        "username": "alice",
        "email": "alice@example.com",
        "password": "secure123"
    }
)

result = register_resp.json()
print(result["message"])

# If email verification required
if not result.get("email_verified"):
    print("Check your email and click verification link")
    # After clicking link, user is auto-logged in

# Login (if email verification not required)
login_resp = requests.post(
    f"{API_BASE}/auth/login",
    json={
        "username": "alice",
        "password": "secure123"
    }
)

# Session cookie set automatically
session_cookie = login_resp.cookies.get("session_id")
print(f"Session ID: {session_cookie}")
```

---

### Create and Use API Token

```python
import requests

API_BASE = "http://localhost:28080"

# Login first (or use existing session)
session = requests.Session()
session.post(
    f"{API_BASE}/auth/login",
    json={"username": "alice", "password": "secure123"}
)

# Create token
token_resp = session.post(
    f"{API_BASE}/auth/tokens/create",
    json={"name": "My API Token"}
)

token_data = token_resp.json()
token = token_data["token"]  # hf_abc123...

print(f"Token created: {token}")
print("SAVE THIS TOKEN - you won't see it again!")

# Use token in API requests
headers = {"Authorization": f"Bearer {token}"}

# List repositories
repos_resp = requests.get(
    f"{API_BASE}/api/models",
    headers=headers
)

print(f"Found {len(repos_resp.json())} models")
```

---

### Manage Tokens

```python
# List all tokens
tokens_resp = session.get(f"{API_BASE}/auth/tokens")
tokens = tokens_resp.json()["tokens"]

for t in tokens:
    print(f"Token: {t['name']} (ID: {t['id']})")
    print(f"  Created: {t['created_at']}")
    print(f"  Last used: {t['last_used'] or 'Never'}")

# Revoke old token
old_token_id = tokens[0]["id"]
revoke_resp = session.delete(f"{API_BASE}/auth/tokens/{old_token_id}")
print(revoke_resp.json()["message"])
```

---

### Registration with Invitation

```python
# For invitation-only servers
invitation_token = "invitation_token_from_link"

register_resp = requests.post(
    f"{API_BASE}/auth/register?invitation_token={invitation_token}",
    json={
        "username": "bob",
        "email": "bob@example.com",
        "password": "secure456"
    }
)

# If invitation was for joining an organization
# User automatically added to that organization
```

---

## Security

### Password Hashing
- Algorithm: bcrypt
- Salt rounds: Automatic (bcrypt default)
- Passwords never stored in plaintext

### Session Security
- HTTP-only cookies (not accessible via JavaScript)
- SameSite: Lax (CSRF protection)
- Secure flag in production (HTTPS only)
- 30-day expiration (configurable)

### Token Security
- SHA256 hashed in database
- Prefix: `hf_` (64 characters total)
- Never logged in plaintext
- Single-use viewing (on creation only)

### Session Secret
- Random 32-character string
- Used for client-side token encryption
- Unique per session
- Returned on login and token creation

---

## Configuration

### Environment Variables

```bash
# How long a session lasts after its last use (hours); using it renews it
KOHAKU_HUB_SESSION_EXPIRE_HOURS=720  # 30 days default

# The longest a session lives from the login (days); 0 = no limit
KOHAKU_HUB_SESSION_ABSOLUTE_DAYS=365  # default

# Email verification
KOHAKU_HUB_REQUIRE_EMAIL_VERIFICATION=false

# Invitation-only registration
KOHAKU_HUB_INVITATION_ONLY=false

# Session secret (for cookie signing)
KOHAKU_HUB_SESSION_SECRET=change_this_in_production

# Email SMTP (for verification emails)
KOHAKU_HUB_SMTP_HOST=smtp.gmail.com
KOHAKU_HUB_SMTP_PORT=587
KOHAKU_HUB_SMTP_USERNAME=your_email@gmail.com
KOHAKU_HUB_SMTP_PASSWORD=your_app_password
KOHAKU_HUB_SMTP_FROM=noreply@yourdomain.com
```

---

## Best Practices

**For Users:**
- Use strong passwords (mix of letters, numbers, symbols)
- Create tokens for CI/CD (don't use password)
- Revoke unused tokens
- Don't share tokens in public repos

**For Developers:**
- Store tokens in environment variables
- Use session cookies for browser apps
- Use Bearer tokens for API/CLI
- Handle 401/403 errors (prompt re-login)

**For Admins:**
- Enable email verification in production
- Use invitation-only mode for private instances
- Set strong SESSION_SECRET
- Enable HTTPS in production
- Monitor token usage via admin API

---

## Error Responses

**401 Unauthorized:**
```json
{
  "detail": "Invalid username or password"
}
```

**403 Forbidden:**
```json
{
  "detail": "Account is disabled"
}
```

**400 Bad Request:**
```json
{
  "detail": "Username already exists"
}
```

---

## Next Steps

- [User Settings API](./settings.md) - Profile, preferences
- [Organizations API](./organizations.md) - Org membership
- [Invitations API](./invitations.md) - Invite users
- [Admin API](./admin.md) - User management
