# Authentication and Authorization Module

## Overview

The `kohakuhub.auth` module provides a comprehensive authentication and authorization system for Kohaku Hub. It implements both session-based authentication (for web UI) and token-based authentication (for API access), along with role-based access control (RBAC) for repositories and organizations.

## Key Features

- **Dual Authentication Methods**
  - Session-based authentication with HTTP-only cookies (web UI)
  - Token-based authentication with Bearer tokens (API access)

- **User Registration and Login**
  - Secure password hashing using bcrypt
  - Username and email uniqueness validation
  - Normalized name conflict detection with organizations

- **Email Verification**
  - Optional email verification requirement (configurable)
  - Time-limited verification tokens (24-hour expiry)
  - HTML and plain-text email support via SMTP
  - Automatic login after successful verification

- **Invitation System**
  - Optional invitation-only registration
  - Invitations for joining organizations with specific roles
  - Multi-use and expiring invitation links

- **API Token Management**
  - User-managed API tokens for programmatic access
  - Token hashing using SHA3-512
  - Per-token naming and usage tracking
  - Token creation and revocation endpoints

- **Role-Based Access Control**
  - Namespace-level permissions (user/organization)
  - Repository-level permissions (read/write/delete)
  - Organization role hierarchy (visitor/member/admin/super-admin)
  - Public vs. private repository access control

## Module Components

### `__init__.py`

Module initialization that exports the primary authentication interface:

- `router` - FastAPI router containing all authentication endpoints
- `get_current_user` - Dependency for protected endpoints requiring authentication
- `get_optional_user` - Dependency for endpoints with optional authentication

### `dependencies.py`

FastAPI dependency injection functions for authentication:

**Key Functions:**

- `get_current_user(session_id, authorization)` - Authenticates users via session cookies or Bearer tokens. Validates session/token expiry and user active status. Returns authenticated `User` object or raises 401 HTTPException.

- `get_optional_user(session_id, authorization)` - Same as `get_current_user` but returns `None` instead of raising an exception when authentication fails. Used for endpoints that work differently for authenticated vs. anonymous users.

**Authentication Flow:**
1. Checks for session cookie first (web UI priority)
2. Falls back to Authorization header with Bearer token (API)
3. Validates expiry timestamps (timezone-aware)
4. Verifies user is active
5. Updates token last_used timestamp on successful API auth
6. Provides detailed debug logging for troubleshooting

### `email.py`

Email verification system implementation:

**Key Functions:**

- `send_verification_email(to_email, username, token)` - Sends HTML and plain-text verification emails via SMTP. Returns `True` on success, `False` on failure.

**Features:**
- SMTP configuration support (host, port, TLS, authentication)
- Graceful degradation when SMTP is disabled (logs verification link)
- Table-based HTML email layout for maximum client compatibility
- Inline CSS styling (required for email clients)
- Gradient header design with emoji indicators
- 24-hour expiration warning
- Fallback plain-text version for non-HTML clients
- Comprehensive error logging

### `permissions.py`

Authorization and permission checking utilities:

**Key Functions:**

- `check_namespace_permission(namespace, user, require_admin)` - Validates if user can use a namespace (username or organization). Checks organization membership and optionally requires admin role.

- `check_repo_read_permission(repo, user)` - Determines if user can read a repository. Public repos are accessible to all; private repos require ownership or organization membership.

- `check_repo_write_permission(repo, user)` - Determines if user can modify a repository. Requires ownership or organization membership with member/admin/super-admin role.

- `check_repo_delete_permission(repo, user)` - Determines if user can delete a repository. Requires ownership or organization admin/super-admin role.

**Permission Model:**
- User namespace: Full control over own resources
- Organization roles:
  - **visitor**: Read-only access
  - **member**: Read and write access
  - **admin**: Read, write, and delete access
  - **super-admin**: Full administrative privileges
- Private repositories require explicit membership
- Public repositories are readable by all users

### `utils.py`

Core cryptographic and utility functions:

**Key Functions:**

- `hash_password(password)` - Hashes passwords using bcrypt with automatic salt generation. Returns base64-encoded hash string.

- `verify_password(password, password_hash)` - Verifies password against stored hash using constant-time comparison.

- `generate_token()` - Generates cryptographically secure random tokens (32 bytes = 64 hex characters) using `secrets.token_hex()`.

- `hash_token(token)` - Hashes tokens using SHA3-512 for secure storage. Stored hashes prevent token recovery from database breaches.

- `generate_session_secret()` - Generates 16-byte random secrets for session token encryption.

- `get_expiry_time(hours)` - Calculates timezone-aware UTC expiry timestamps.

**Security Properties:**
- Bcrypt for password hashing (adaptive rounds, salt per password)
- SHA3-512 for token hashing (one-way, collision-resistant)
- CSPRNG-based token generation (secrets module)
- Timezone-aware datetime handling (prevents timezone bugs)

### `routes.py`

FastAPI routes implementing authentication endpoints:

**Endpoints:**

- `POST /auth/register` - User registration with username, email, and password. Supports an optional `invitation_token` for invitation-only mode. Validates uniqueness, checks normalized name conflicts with users and organizations. Creates user atomically and optionally sends verification email.

- `GET /auth/verify-email?token=<token>` - Email verification endpoint. Validates token, marks email as verified, creates login session, and redirects to user profile with session cookie set.

- `POST /auth/login` - User login with username and password. Validates credentials, checks account status and email verification (if required). Creates session and sets HTTP-only cookie.

- `POST /auth/logout` - Destroys all user sessions and clears session cookie.

- `GET /auth/me` - Returns current user information (requires authentication).

- `GET /auth/tokens` - Lists user's API tokens with metadata (ID, name, last used, created).

- `POST /auth/tokens/create` - Creates new API token. Returns token string (only shown once) and token metadata.

- `DELETE /auth/tokens/{token_id}` - Revokes and deletes an API token.

**Request/Response Models:**
- `RegisterRequest`: username, email, password
- `LoginRequest`: username, password
- `CreateTokenRequest`: name

## System Integration

### FastAPI Dependencies

The module provides FastAPI dependency functions that can be used in route handlers:

```python
from kohakuhub.auth import get_current_user, get_optional_user
from kohakuhub.db import User

@router.get("/protected")
def protected_endpoint(user: User = Depends(get_current_user)):
    # user is guaranteed to be authenticated
    return {"username": user.username}

@router.get("/public")
def public_endpoint(user: User = Depends(get_optional_user)):
    # user may be None for anonymous access
    if user:
        return {"message": f"Hello {user.username}"}
    return {"message": "Hello anonymous"}
```

### Database Models

The module interacts with these database models:

- **User**: Stores user credentials, email verification status, quota information
- **Session**: Stores web UI sessions with expiry and encryption secrets
- **Token**: Stores API token hashes with names and usage tracking
- **EmailVerification**: Stores time-limited email verification tokens
- **UserOrganization**: Stores organization memberships with roles

### Configuration

Authentication behavior is controlled by configuration settings:

```python
from kohakuhub.config import cfg

# Email verification
cfg.auth.require_email_verification  # Enable/disable email verification

# Session management
cfg.auth.session_expire_hours   # Session lifetime after its last use (renewed on use)
cfg.auth.session_absolute_days  # Longest a session lives from the login (0 = no limit)

# SMTP settings (for email)
cfg.smtp.enabled  # Enable/disable email sending
cfg.smtp.host     # SMTP server
cfg.smtp.port     # SMTP port
cfg.smtp.use_tls  # Enable TLS
cfg.smtp.username # SMTP authentication
cfg.smtp.password # SMTP password
cfg.smtp.from_email  # Sender address

# Application settings
cfg.app.base_url  # Base URL for verification links
```

## Authentication Flow

### Web UI Authentication (Session-based)

1. **Registration:**
   - User submits username, email, password
   - System validates uniqueness and creates user
   - If email verification required: sends verification email
   - User clicks email link → creates session → redirects to profile

2. **Login:**
   - User submits username and password
   - System verifies credentials and account status
   - Creates session with random session_id and secret
   - Sets HTTP-only cookie with session_id
   - Cookie lifetime: session_expire_hours, renewed while the session is used
     (see `auth/session.py`), never past session_absolute_days from the login

3. **Authenticated Requests:**
   - Browser sends session_id cookie automatically
   - `get_current_user` validates session and expiry
   - Returns User object for use in route handlers
   - A session used more than an hour after its last renewal is renewed: the
     expiry moves forward and the cookie is sent again with the new lifetime

4. **Logout:**
   - Deletes all user sessions from database
   - Clears session_id cookie

### API Authentication (Token-based)

1. **Token Creation:**
   - Authenticated user creates token via web UI or API
   - System generates random token string (shown once)
   - Stores SHA3-512 hash in database with name
   - User saves token securely

2. **API Requests:**
   - Client sends `Authorization: Bearer <token>` header
   - `get_current_user` hashes token and looks up in database
   - Updates last_used timestamp
   - Returns User object for use in route handlers

3. **Token Revocation:**
   - User deletes token via web UI or API
   - Token hash removed from database
   - Token immediately becomes invalid

## Authorization Mechanisms

### Permission Checking Pattern

Permission checking functions raise `HTTPException(403)` on denial:

```python
from kohakuhub.auth.permissions import check_repo_write_permission

@router.post("/repos/{namespace}/{name}/files")
def upload_file(
    namespace: str,
    name: str,
    user: User = Depends(get_current_user)
):
    repo = get_repository(namespace, name)
    check_repo_write_permission(repo, user)  # Raises 403 if denied
    # ... proceed with upload
```

### Namespace Ownership

- **User namespace**: `username` matches `user.username`
- **Organization namespace**: User is member of organization with name matching `namespace`

### Repository Access Levels

| Action | Owner | Org Admin | Org Member | Org Visitor | Anonymous |
|--------|-------|-----------|------------|-------------|-----------|
| Read Public | Yes | Yes | Yes | Yes | Yes |
| Read Private | Yes | Yes | Yes | Yes | No |
| Write | Yes | Yes | Yes | No | No |
| Delete | Yes | Yes | No | No | No |

### Organization Roles

- **visitor**: Can read organization repositories (public and private)
- **member**: Can read and write to organization repositories
- **admin**: Can read, write, and delete organization repositories
- **super-admin**: Full administrative control (not currently differentiated from admin in permission checks)

## Security Considerations

- **Password Storage**: Bcrypt hashing with per-password salts
- **Token Storage**: SHA3-512 hashing prevents token recovery from database
- **Session Security**: HTTP-only cookies prevent XSS token theft
- **Token Generation**: CSPRNG ensures unpredictable tokens
- **Constant-Time Comparison**: Bcrypt verification prevents timing attacks
- **Timezone Handling**: UTC timestamps prevent timezone-related vulnerabilities
- **Input Validation**: Pydantic models validate all request data
- **Atomic Operations**: Database transactions ensure consistency
- **Rate Limiting**: Should be implemented at reverse proxy level (not in module)

## Logging

The module provides detailed logging for security auditing:

- Authentication attempts (session and token)
- Registration and login successes/failures
- Session creation and destruction
- Token usage tracking
- Email verification events
- Permission denial reasons

Logs include truncated session IDs and token IDs for privacy while maintaining traceability.
