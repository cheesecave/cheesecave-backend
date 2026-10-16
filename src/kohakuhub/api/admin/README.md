# Admin API Module

## Overview

The `kohakuhub.api.admin` module provides a secure, token-protected RESTful API for administrative management of a KohakuHub instance. It offers a comprehensive set of endpoints for managing users, repositories, storage quotas, and system-wide settings.

## Key Features

- **User and Organization Management**: Create, list, view, and delete users and organizations.
- **Quota Management**: Set and monitor storage quotas for both users and organizations, with separate limits for private and public repositories.
- **Repository Management**: List and inspect repositories, view storage breakdowns, and manage repository metadata.
- **System Statistics**: Access detailed system-wide statistics, including time-series data for monitoring and analytics.
- **Database Viewer**: A read-only interface to execute safe `SELECT` queries against the database for debugging and inspection.
- **Fallback Source Management**: Configure and manage external repository sources for the fallback system.
- **Invitation Management**: Create and manage user registration invitations, essential for invitation-only deployments.
- **Secure Authentication**: All endpoints are protected by a secret token, with hash-based verification to prevent timing attacks.

## Module Structure

The admin module is organized into several routers, each handling a specific area of administration:

- `commits.py`: Endpoints for listing and filtering commits.
- `database.py`: Read-only SQL query execution and schema viewing.
- `fallback.py`: Management of fallback sources.
- `invitations.py`: Creation and management of user invitations.
- `quota.py`: Endpoints for managing user and repository quotas.
- `repositories.py`: Repository inspection and management.
- `search.py`: Global search functionality across different resources.
- `stats.py`: Endpoints for retrieving system statistics.
- `storage.py`: S3 storage browsing and debugging.
- `users.py`: User and organization management.

## Authentication

Access to the admin API is protected by a secret token. The token must be provided in the `X-Admin-Token` header of each request. The server verifies the token by comparing SHA3-512 hashes in constant time to prevent timing attacks.

The admin token is configured via the `CHEESE_CAVE_ADMIN_SECRET_TOKEN` environment variable.

## API Endpoints

All admin endpoints are prefixed with `/admin/api`.

### Users
- `GET /users`: List users.
- `POST /users`: Create a new user.
- `GET /users/{username}`: Get detailed information about a user.
- `DELETE /users/{username}`: Delete a user.
- `PATCH /users/{username}/email-verification`: Set a user's email verification status.
- `PUT /users/{username}/quota`: Update a user's storage quota.

### Repositories
- `GET /repositories`: List all repositories.
- `GET /repositories/{repo_type}/{namespace}/{name}`: Get detailed information about a repository.
- `GET /repositories/{repo_type}/{namespace}/{name}/files`: Get a list of files in a repository.
- `GET /repositories/{repo_type}/{namespace}/{name}/storage-breakdown`: Get a storage analytics breakdown for a repository.

### Quotas
- `GET /quota/overview`: Get a system-wide quota overview.
- `GET /quota/{namespace}`: Get quota information for a user or organization.
- `PUT /quota/{namespace}`: Set the storage quota for a user or organization.
- `POST /quota/{namespace}/recalculate`: Recalculate storage usage for a user or organization.
- `POST /repositories/recalculate-all`: Recalculate storage for all repositories.

### Database
- `GET /database/tables`: List all database tables and their schemas.
- `GET /database/templates`: Get pre-defined safe query templates.
- `POST /database/query`: Execute a read-only SQL query.

### Other
- `GET /stats`: Get basic system statistics.
- `GET /stats/detailed`: Get detailed system statistics.
- `GET /stats/timeseries`: Get time-series statistics for charts.
- `GET /stats/top-repos`: Get top repositories by commits or size.
- `GET /search`: Perform a global search.
- `POST /invitations/register`: Create a registration invitation.
- `GET /invitations`: List all invitations.
- `DELETE /invitations/{token}`: Delete an invitation.
- `POST /fallback-sources`: Create a fallback source.
- `GET /fallback-sources`: List fallback sources.
- `GET /fallback-sources/{source_id}`: Get a specific fallback source.
- `PUT /fallback-sources/{source_id}`: Update a fallback source.
- `DELETE /fallback-sources/{source_id}`: Delete a fallback source.
- `GET /storage/buckets`: List S3 buckets.
- `GET /storage/objects`: List objects in an S3 bucket.
