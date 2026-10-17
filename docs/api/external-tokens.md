---
title: External Tokens API
description: Manage external fallback tokens for accessing remote sources
icon: key
---

# External Tokens API

Manage user-specific external tokens for accessing fallback sources like HuggingFace Hub.

## Overview

KohakuHub supports **external fallback sources** for repositories not available locally. Users can provide their own tokens to access private repositories on external sources.

**Token Storage:**
- **Database**: Encrypted tokens stored persistently (AES-256)
- **Authorization header**: Temporary per-request tokens
- **Priority**: Header tokens override database tokens

**Security:**
- All tokens encrypted with `DATABASE_KEY` (Fernet/AES-256)
- Tokens never logged in plaintext
- Masked when displayed (e.g., `hf_a***`)

---

## Endpoints

### Get Available Fallback Sources

Get list of configured fallback sources (public endpoint).

**Endpoint:** `GET /api/fallback-sources/available`

**Authentication:** None (public)

**Response:**

```json
[
  {
    "url": "https://huggingface.co",
    "name": "HuggingFace Hub",
    "source_type": "huggingface",
    "priority": 10
  },
  {
    "url": "https://custom-hub.example.com",
    "name": "Custom Hub",
    "source_type": "huggingface",
    "priority": 20
  }
]
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `url` | string | Base URL of the fallback source |
| `name` | string | Display name |
| `source_type` | string | Source type (currently: `huggingface`) |
| `priority` | integer | Priority (lower = higher priority) |

**Use Cases:**

- Display available sources to users
- Let users know which sources they can configure tokens for
- Show source priorities

**Example:**

```python
import requests

response = requests.get("http://localhost:28080/api/fallback-sources/available")
sources = response.json()

print("Available fallback sources:")
for source in sources:
    print(f"  {source['name']}: {source['url']}")
```

---

### List User External Tokens

Get list of user's configured external tokens (tokens are masked).

**Endpoint:** `GET /api/users/{username}/external-tokens`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username (must match authenticated user) |

**Authentication:** Required (can only view own tokens)

**Response:**

```json
[
  {
    "url": "https://huggingface.co",
    "token_preview": "hf_a***",
    "created_at": "2025-01-15T10:30:00Z",
    "updated_at": "2025-01-15T10:30:00Z"
  },
  {
    "url": "https://custom-hub.example.com",
    "token_preview": "tok_x***",
    "created_at": "2025-01-10T08:00:00Z",
    "updated_at": "2025-01-12T14:30:00Z"
  }
]
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `url` | string | Fallback source URL |
| `token_preview` | string | Masked token (first 4 chars + `***`) |
| `created_at` | string | ISO 8601 timestamp when token was added |
| `updated_at` | string | ISO 8601 timestamp when token was last updated |

**Security:**

- Full tokens are never returned in API responses
- Only first 4 characters shown (e.g., `hf_a***`)
- Tokens are encrypted in database

**Example:**

```python
response = requests.get(
    "http://localhost:28080/api/users/alice/external-tokens",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
tokens = response.json()

for token in tokens:
    print(f"{token['url']}: {token['token_preview']}")
```

---

### Add or Update External Token

Add a new external token or update an existing one.

**Endpoint:** `POST /api/users/{username}/external-tokens`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username (must match authenticated user) |

**Request Body:**

```json
{
  "url": "https://huggingface.co",
  "token": "hf_abc123xyz789..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | Base URL of fallback source (must start with `http`) |
| `token` | string | Yes | Plain text token |

**Authentication:** Required (can only manage own tokens)

**Response:**

```json
{
  "success": true,
  "message": "External token saved"
}
```

**Behavior:**

- If token exists for this URL, it will be updated
- If token doesn't exist, it will be created
- Token is encrypted before storage
- URL must be a valid HTTP(S) URL

**Example:**

```python
# Add HuggingFace token
response = requests.post(
    "http://localhost:28080/api/users/alice/external-tokens",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "url": "https://huggingface.co",
        "token": "hf_abc123xyz789..."
    }
)

# Add custom hub token
response = requests.post(
    "http://localhost:28080/api/users/alice/external-tokens",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "url": "https://custom-hub.example.com",
        "token": "custom_token_here"
    }
)
```

---

### Delete External Token

Delete an external token for a specific URL.

**Endpoint:** `DELETE /api/users/{username}/external-tokens/{url:path}`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username (must match authenticated user) |
| `url` | string | path | Yes | URL of the fallback source (URL-encoded) |

**Authentication:** Required (can only manage own tokens)

**Response:**

```json
{
  "success": true,
  "message": "External token deleted"
}
```

**Error Responses:**

| Status | Description |
|--------|-------------|
| 403 | Not authorized to manage these tokens |
| 404 | Token not found for this URL |

**Example:**

```python
import urllib.parse

# URL-encode the source URL
url = urllib.parse.quote("https://huggingface.co", safe='')

response = requests.delete(
    f"http://localhost:28080/api/users/alice/external-tokens/{url}",
    headers={"Authorization": "Bearer YOUR_TOKEN"}
)
```

---

### Bulk Update External Tokens

Replace all external tokens with a new set (useful for sync operations).

**Endpoint:** `PUT /api/users/{username}/external-tokens/bulk`

**Parameters:**

| Parameter | Type | Location | Required | Description |
|-----------|------|----------|----------|-------------|
| `username` | string | path | Yes | Username (must match authenticated user) |

**Request Body:**

```json
{
  "tokens": [
    {
      "url": "https://huggingface.co",
      "token": "hf_abc123..."
    },
    {
      "url": "https://custom-hub.example.com",
      "token": "custom_token..."
    }
  ]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tokens` | array | Yes | List of token objects |
| `tokens[].url` | string | Yes | Source URL |
| `tokens[].token` | string | Yes | Plain text token |

**Authentication:** Required (can only manage own tokens)

**Response:**

```json
{
  "success": true,
  "message": "Updated 2 external tokens"
}
```

**Behavior:**

- Deletes tokens not in the new list
- Adds new tokens
- Updates existing tokens
- Atomic operation (all or nothing)

**Use Cases:**

- Sync tokens from frontend localStorage
- Import/export token configurations
- Reset all tokens

**Example:**

```python
# Replace all tokens
response = requests.put(
    "http://localhost:28080/api/users/alice/external-tokens/bulk",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "tokens": [
            {
                "url": "https://huggingface.co",
                "token": "hf_new_token_123"
            },
            {
                "url": "https://custom.example.com",
                "token": "custom_token_456"
            }
        ]
    }
)
```

---

## Authorization Header Format

External tokens can also be passed per-request via the Authorization header.

**Format:** `Bearer <auth_token>|<url1>,<token1>|<url2>,<token2>...`

**Examples:**

```
# Regular authentication only
Authorization: Bearer hf_abc123

# With one external token
Authorization: Bearer hf_abc123|https://huggingface.co,hf_ext123

# With multiple external tokens
Authorization: Bearer hf_abc123|https://huggingface.co,hf_ext123|https://custom.com,tok_xyz

# Session-based auth with external token (no main token)
Authorization: Bearer |https://huggingface.co,hf_ext123

# Empty token allowed (for public sources with auth requirement)
Authorization: Bearer hf_abc123|https://example.com,
```

**Priority:**

1. **Header tokens** (highest) - Temporary, per-request
2. **Database tokens** - Persistent user preferences
3. **Admin tokens** (lowest) - Server-wide defaults

**Use Cases:**

- Temporary token override without saving
- Anonymous users passing tokens
- CI/CD with different tokens per job

---

## Usage Examples

### Comprehensive Token Management

```python
import requests
import urllib.parse

BASE_URL = "http://localhost:28080"
TOKEN = "YOUR_TOKEN"

class ExternalTokenManager:
    def __init__(self, base_url: str, token: str, username: str):
        self.base_url = base_url
        self.username = username
        self.headers = {"Authorization": f"Bearer {token}"}

    def list_sources(self):
        """List available fallback sources."""
        response = requests.get(
            f"{self.base_url}/api/fallback-sources/available"
        )
        response.raise_for_status()
        return response.json()

    def list_tokens(self):
        """List user's external tokens."""
        response = requests.get(
            f"{self.base_url}/api/users/{self.username}/external-tokens",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def add_token(self, url: str, token: str):
        """Add or update external token."""
        response = requests.post(
            f"{self.base_url}/api/users/{self.username}/external-tokens",
            headers=self.headers,
            json={"url": url, "token": token}
        )
        response.raise_for_status()
        return response.json()

    def delete_token(self, url: str):
        """Delete external token."""
        encoded_url = urllib.parse.quote(url, safe='')
        response = requests.delete(
            f"{self.base_url}/api/users/{self.username}/external-tokens/{encoded_url}",
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def bulk_update(self, tokens: list[dict]):
        """Bulk update all tokens."""
        response = requests.put(
            f"{self.base_url}/api/users/{self.username}/external-tokens/bulk",
            headers=self.headers,
            json={"tokens": tokens}
        )
        response.raise_for_status()
        return response.json()

    def print_summary(self):
        """Print comprehensive token summary."""
        print(f"\n=== External Tokens for {self.username} ===\n")

        # Available sources
        sources = self.list_sources()
        print("Available sources:")
        for source in sources:
            print(f"  {source['name']}: {source['url']}")

        # User's tokens
        tokens = self.list_tokens()
        print(f"\nConfigured tokens ({len(tokens)}):")
        for token in tokens:
            print(f"  {token['url']}: {token['token_preview']}")
            print(f"    Updated: {token['updated_at']}")

# Usage
mgr = ExternalTokenManager(BASE_URL, TOKEN, "alice")

# Add tokens
mgr.add_token("https://huggingface.co", "hf_abc123xyz789...")
mgr.add_token("https://custom.example.com", "custom_token_here")

# List tokens
mgr.print_summary()

# Delete token
mgr.delete_token("https://custom.example.com")

# Bulk update (sync from another source)
mgr.bulk_update([
    {"url": "https://huggingface.co", "token": "hf_new_token"},
    {"url": "https://another.com", "token": "another_token"}
])
```

### Using Header-Based Tokens

```python
def make_request_with_external_token(repo_id: str, external_url: str, external_token: str):
    """Make request with temporary external token in header."""

    # Format: Bearer <main_token>|<external_url>,<external_token>
    auth_header = f"Bearer {TOKEN}|{external_url},{external_token}"

    response = requests.get(
        f"{BASE_URL}/models/{repo_id}/tree/main",
        headers={"Authorization": auth_header}
    )

    return response.json()

# Usage - temporary HF token for this request only
files = make_request_with_external_token(
    "openai/gpt-4",
    "https://huggingface.co",
    "hf_temporary_token_123"
)
```

### Frontend Integration (JavaScript)

```javascript
class ExternalTokenManager {
  constructor(baseURL, token, username) {
    this.baseURL = baseURL;
    this.username = username;
    this.headers = {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    };
  }

  async listSources() {
    const response = await fetch(
      `${this.baseURL}/api/fallback-sources/available`
    );
    return await response.json();
  }

  async listTokens() {
    const response = await fetch(
      `${this.baseURL}/api/users/${this.username}/external-tokens`,
      { headers: this.headers }
    );
    return await response.json();
  }

  async addToken(url, token) {
    const response = await fetch(
      `${this.baseURL}/api/users/${this.username}/external-tokens`,
      {
        method: 'POST',
        headers: this.headers,
        body: JSON.stringify({ url, token })
      }
    );
    return await response.json();
  }

  async deleteToken(url) {
    const encodedUrl = encodeURIComponent(url);
    const response = await fetch(
      `${this.baseURL}/api/users/${this.username}/external-tokens/${encodedUrl}`,
      {
        method: 'DELETE',
        headers: this.headers
      }
    );
    return await response.json();
  }

  async bulkUpdate(tokens) {
    const response = await fetch(
      `${this.baseURL}/api/users/${this.username}/external-tokens/bulk`,
      {
        method: 'PUT',
        headers: this.headers,
        body: JSON.stringify({ tokens })
      }
    );
    return await response.json();
  }

  // Format authorization header with external tokens
  formatAuthHeader(mainToken, externalTokens) {
    if (!externalTokens || externalTokens.length === 0) {
      return `Bearer ${mainToken}`;
    }

    const tokenPairs = externalTokens
      .map(t => `${t.url},${t.token}`)
      .join('|');

    return `Bearer ${mainToken}|${tokenPairs}`;
  }
}

// Usage
const tokenMgr = new ExternalTokenManager(
  'http://localhost:28080',
  'YOUR_TOKEN',
  'alice'
);

// Add token
await tokenMgr.addToken('https://huggingface.co', 'hf_abc123...');

// List tokens
const tokens = await tokenMgr.listTokens();
console.log('Configured tokens:', tokens);

// Make request with external tokens
const externalTokens = [
  { url: 'https://huggingface.co', token: 'hf_abc123' }
];
const authHeader = tokenMgr.formatAuthHeader('main_token', externalTokens);

const response = await fetch('/models/openai/gpt-4/tree/main', {
  headers: { 'Authorization': authHeader }
});
```

### Sync Between LocalStorage and Database

```javascript
// Frontend utility for dual storage
class TokenStorage {
  constructor(apiManager) {
    this.api = apiManager;
    this.localStorageKey = 'kohakuhub_external_tokens';
  }

  // Get from localStorage
  getLocal() {
    const stored = localStorage.getItem(this.localStorageKey);
    return stored ? JSON.parse(stored) : [];
  }

  // Save to localStorage
  saveLocal(tokens) {
    localStorage.setItem(this.localStorageKey, JSON.stringify(tokens));
  }

  // Add token to both localStorage and database
  async add(url, token) {
    // Update localStorage
    const tokens = this.getLocal();
    const existingIndex = tokens.findIndex(t => t.url === url);

    if (existingIndex >= 0) {
      tokens[existingIndex] = { url, token };
    } else {
      tokens.push({ url, token });
    }

    this.saveLocal(tokens);

    // Update database
    await this.api.addToken(url, token);
  }

  // Sync localStorage to database
  async syncToDatabase() {
    const localTokens = this.getLocal();
    await this.api.bulkUpdate(localTokens);
  }

  // Sync database to localStorage
  async syncFromDatabase() {
    const dbTokens = await this.api.listTokens();

    // Convert from preview format to full tokens (only if we have full tokens)
    // Note: We can't recover full tokens from preview, so this only works
    // if we're maintaining localStorage as source of truth
    const localTokens = this.getLocal();

    // Merge: keep local tokens that match DB URLs
    const merged = localTokens.filter(lt =>
      dbTokens.some(dt => dt.url === lt.url)
    );

    this.saveLocal(merged);
  }

  // Clear all tokens
  async clear() {
    localStorage.removeItem(this.localStorageKey);
    const dbTokens = await this.api.listTokens();
    for (const token of dbTokens) {
      await this.api.deleteToken(token.url);
    }
  }
}

// Usage
const storage = new TokenStorage(tokenMgr);

// Add token to both
await storage.add('https://huggingface.co', 'hf_abc123...');

// Sync localStorage to DB
await storage.syncToDatabase();
```

---

## Security Best Practices

### Token Encryption

- All tokens encrypted with AES-256 (Fernet)
- Encryption key derived from `DATABASE_KEY` environment variable
- Never store tokens in plaintext
- Tokens masked when displayed

### Token Handling

```python
from kohakuhub.crypto import encrypt_token, decrypt_token, mask_token

# Encrypt before storage
encrypted = encrypt_token("hf_abc123xyz789...")

# Decrypt when reading
plain = decrypt_token(encrypted)

# Mask for display
masked = mask_token("hf_abc123xyz789...", show_chars=4)
# Returns: "hf_a***"
```

### Environment Setup

```bash
# Generate secure encryption key
export CHEESE_CAVE_DATABASE_KEY="$(openssl rand -hex 32)"

# Optional: Require authentication for fallback access
export CHEESE_CAVE_FALLBACK_REQUIRE_AUTH=false
```

---

## CLI Usage

See [CLI Documentation](../CLI.md#external-tokens) for command-line interface:

```bash
# List available sources
kohub-cli settings user external-tokens sources

# List configured tokens
kohub-cli settings user external-tokens list

# Add token
kohub-cli settings user external-tokens add \
  --url https://huggingface.co \
  --token hf_abc123...

# Delete token
kohub-cli settings user external-tokens delete \
  --url https://huggingface.co

# Bulk import from file
kohub-cli settings user external-tokens import tokens.json
```

---

## Next Steps

- [Settings API](./settings.md) - User and organization settings
- [Repository API](../API.md#repositories) - Access repositories
- [Authentication](../API.md#authentication) - Token-based authentication
- [Fallback System](../deployment.md#fallback-configuration) - Configure fallback sources
