# KohakuHub API Layer

## Overview

The `kohakuhub.api` module is the core API layer of KohakuHub, providing a comprehensive RESTful API for Git-like version control, repository management, and large file storage. KohakuHub implements a HuggingFace Hub-compatible API while leveraging LakeFS for data lake capabilities and S3 for object storage, enabling seamless integration with existing ML tools and workflows.

This module serves as the primary interface between client applications (web UI, CLI, Git clients, HuggingFace Hub clients) and the underlying storage infrastructure (LakeFS, S3, PostgreSQL).

## Architecture Overview

### High-Level System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Client Layer                                │
│  (Web UI, CLI, Git Client, HuggingFace Hub Python Client)          │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       KohakuHub API Layer                            │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────────────┐  │
│  │   Repo   │  Commit  │   Git    │   Org    │  Quota & Utils   │  │
│  │   CRUD   │  Mgmt    │ Protocol │   Mgmt   │                  │  │
│  └──────────┴──────────┴──────────┴──────────┴──────────────────┘  │
└────────────────────────────┬────────────────────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
      ┌──────────────┐ ┌──────────┐ ┌──────────────┐
      │   Database   │ │  LakeFS  │ │  S3 Storage  │
      │ (PostgreSQL) │ │          │ │              │
      └──────────────┘ └──────────┘ └──────────────┘
```

### Data Flow Architecture

```
Upload Flow:
  Client → Auth → Quota Check → Repository Validation →
  LakeFS Staging → Commit → S3 Storage → Database Update → Response

Download Flow:
  Client → Auth → Permission Check → LakeFS Metadata →
  S3 Presigned URL → Redirect → Direct S3 Download

Git Clone Flow:
  Git Client → Git HTTP Protocol → LakeFS Bridge →
  Dynamic Pack Generation → Stream to Client
```

## Core Modules

### 1. Repository Management (`repo/`)

**Purpose**: Complete repository lifecycle management and browsing

**Key Responsibilities**:
- Repository CRUD operations (create, delete, move, squash)
- Repository information retrieval and search
- File tree browsing and directory navigation
- HuggingFace Hub API compatibility
- Storage cleanup and garbage collection

**Main Components**:
- `routers/crud.py`: Repository creation, deletion, moving, and squashing
- `routers/info.py`: Repository metadata and listing endpoints
- `routers/tree.py`: File tree navigation and path information
- `utils/hf.py`: HuggingFace Hub compatibility layer
- `utils/gc.py`: Garbage collection utilities for LFS objects

**Integration Points**:
- Database: Repository, File, Commit models
- LakeFS: Repository and branch management
- S3: Object storage cleanup
- Quota: Storage tracking and enforcement

**API Endpoints**:
```
POST   /repos/create                              - Create repository
DELETE /repos/delete                              - Delete repository
POST   /repos/move                                - Move repository
POST   /repos/squash                              - Squash commits
GET    /{repo_type}s/{namespace}/{name}          - Get repository info
GET    /{repo_type}s                             - List repositories
GET    /{repo_type}s/{namespace}/{name}/tree/... - Browse file tree
POST   /{repo_type}s/{namespace}/{name}/paths-info/... - Get path info
```

### 2. Commit Management (`commit/`)

**Purpose**: Git-like atomic commit operations and history

**Key Responsibilities**:
- Atomic multi-operation commits
- Commit history retrieval with pagination
- Commit diff generation
- LFS object tracking and linking
- Deduplication and storage optimization

**Main Components**:
- `routers/operations.py`: Commit creation and file operations (upload, delete, copy)
- `routers/history.py`: Commit history and diff endpoints

**Supported Operations**:
- `file`: Inline base64-encoded small files
- `lfsFile`: Large files via Git LFS workflow
- `deletedFile`: Individual file deletion
- `deletedFolder`: Recursive directory deletion
- `copyFile`: File copying across revisions

**Integration Points**:
- Database: Commit, File, LFSObjectHistory models
- LakeFS: Commit and object operations
- S3: LFS object storage
- Quota: Storage usage updates

**API Endpoints**:
```
POST /api/{repo_type}s/{namespace}/{name}/commit/{revision}         - Create commit
GET  /api/{repo_type}s/{namespace}/{name}/commits/{branch}          - List commits
GET  /api/{repo_type}s/{namespace}/{name}/commit/{commit_id}        - Get commit
GET  /api/{repo_type}s/{namespace}/{name}/commit/{commit_id}/diff   - Get diff
```

### 3. Git Protocol Support (`git/`)

**Purpose**: Native Git client operations and LFS support

**Key Responsibilities**:
- Git Smart HTTP protocol implementation
- Git LFS Batch API for large file uploads/downloads
- SSH key management
- Pure Python Git-LakeFS bridge (no native Git binaries)
- Presigned S3 URL generation for direct transfers

**Main Components**:
- `routers/http.py`: Git Smart HTTP protocol endpoints
- `routers/lfs.py`: Git LFS Batch API implementation
- `routers/ssh_keys.py`: SSH key CRUD operations
- `utils/server.py`: Git protocol utilities (pkt-line, upload-pack, receive-pack)
- `utils/lakefs_bridge.py`: Git-LakeFS bridge with dynamic pack generation
- `utils/objects.py`: Pure Python Git object construction

**Git Operations Supported**:
- Clone, fetch, pull (git-upload-pack)
- Push (git-receive-pack, partial)
- LFS batch upload/download
- SSH authentication

**Integration Points**:
- Database: Repository, User, SSHKey models
- LakeFS: Object storage and version control
- S3: LFS object storage with presigned URLs
- Quota: Upload quota enforcement

**API Endpoints**:
```
GET  /{namespace}/{name}.git/info/refs              - Git service advertisement
POST /{namespace}/{name}.git/git-upload-pack        - Git fetch/pull
POST /{namespace}/{name}.git/git-receive-pack       - Git push
POST /{namespace}/{name}.git/info/lfs/objects/batch - LFS batch API
POST /api/{namespace}/{name}.git/info/lfs/verify    - LFS verification
GET  /api/user/keys                                 - List SSH keys
POST /api/user/keys                                 - Add SSH key
```

### 4. Organization Management (`org/`)

**Purpose**: Multi-user collaboration and team management

**Key Responsibilities**:
- Organization creation with default quotas
- Member lifecycle management
- Role-based access control (RBAC)
- Organization metadata management

**Role Hierarchy**:
- `super-admin`: Organization creator with full privileges
- `admin`: Administrative privileges for member management
- `member`: Basic organization membership

**Main Components**:
- `router.py`: Organization and membership API endpoints
- `util.py`: Core business logic for organization operations

**Integration Points**:
- Database: Organization, User, UserOrganization models
- Quota: Organization storage quotas
- Auth: Permission checking for protected operations

**API Endpoints**:
```
POST   /org/create                              - Create organization
GET    /org/{org_name}                          - Get organization details
POST   /org/{org_name}/members                  - Add member
DELETE /org/{org_name}/members/{username}       - Remove member
PUT    /org/{org_name}/members/{username}       - Update member role
GET    /org/{org_name}/members                  - List members
GET    /org/users/{username}/orgs               - List user's organizations
```

### 5. Quota Management (`quota/`)

**Purpose**: Storage tracking and enforcement

**Key Responsibilities**:
- Dual-quota system (private/public repositories)
- Real-time storage usage monitoring
- Pre-upload quota validation
- Storage recalculation and synchronization
- Permission-based visibility

**Storage Calculation**:
- Current branch storage (all files in main branch)
- LFS object storage (all versions with history)
- Deduplication awareness
- Incremental updates

**Main Components**:
- `router.py`: Quota API endpoints
- `util.py`: Storage calculation and quota enforcement logic

**Integration Points**:
- Database: User, Organization, Repository, File, LFSObjectHistory models
- LakeFS: Object size retrieval
- Commit/Git: Pre-upload quota checks

**API Endpoints**:
```
GET  /api/quota/{namespace}                 - Get quota information
PUT  /api/quota/{namespace}                 - Set storage quotas
POST /api/quota/{namespace}/recalculate     - Recalculate storage
GET  /api/quota/{namespace}/public          - Get public quota info
```

## Supporting Modules

### File Operations (`files.py`)

**Purpose**: File upload/download orchestration

**Key Features**:
- Preupload endpoint for deduplication checks
- Revision information retrieval
- Presigned S3 URL generation for downloads
- Content-based deduplication (SHA256 and sample comparison)

**API Endpoints**:
```
POST /{repo_type}s/{namespace}/{name}/preupload/{revision}  - Preupload check
GET  /{repo_type}s/{namespace}/{name}/revision/{revision}   - Get revision info
HEAD /{repo_type}s/{namespace}/{name}/resolve/{revision}/... - File metadata
GET  /{repo_type}s/{namespace}/{name}/resolve/{revision}/... - File download
```

### Branch and Tag Management (`branches.py`)

**Purpose**: Git-like branch and tag operations

**Key Features**:
- Branch creation from any revision
- Branch deletion with safety checks
- Tag creation and deletion
- Revert operations with LFS recoverability checks
- Merge operations with conflict detection
- Reset operations with diff-based restoration

**API Endpoints**:
```
POST   /{repo_type}s/{namespace}/{name}/branch                    - Create branch
DELETE /{repo_type}s/{namespace}/{name}/branch/{branch}           - Delete branch
POST   /{repo_type}s/{namespace}/{name}/tag                       - Create tag
DELETE /{repo_type}s/{namespace}/{name}/tag/{tag}                 - Delete tag
POST   /{repo_type}s/{namespace}/{name}/branch/{branch}/revert    - Revert commit
POST   /{repo_type}s/{namespace}/{name}/merge/{src}/into/{dest}   - Merge branches
POST   /{repo_type}s/{namespace}/{name}/branch/{branch}/reset     - Reset branch
```

### Settings Management (`settings.py`)

**Purpose**: User, organization, and repository settings

**Key Features**:
- User profile updates (email, fullname)
- Organization description updates
- Repository visibility and access control
- Quota validation for visibility changes

**API Endpoints**:
```
PUT /api/users/{username}/settings                        - Update user settings
PUT /api/organizations/{org_name}/settings                - Update org settings
PUT /api/{repo_type}s/{namespace}/{name}/settings         - Update repo settings
```

### Name Validation (`validation.py`)

**Purpose**: Name conflict checking and normalization

**Key Features**:
- Exact match detection
- Normalized conflict detection (e.g., 'My-Repo' conflicts with 'my_repo')
- Support for usernames, organizations, and repository names

**API Endpoints**:
```
POST /api/validate/check-name  - Check name availability
```

### Admin Operations (`admin.py`)

**Purpose**: Administrative functions and system management

**Key Features**:
- User management (super-admin only)
- System-wide quota operations
- Administrative overrides

## System Integration

### Authentication and Authorization

**Authentication Layer** (`auth/`):
- JWT token-based authentication
- User session management
- Token refresh and rotation
- Public/private access control

**Permission System**:
- `check_repo_read_permission()`: Public repos or authenticated owner/org member
- `check_repo_write_permission()`: Authenticated owner or org admin/member
- `check_repo_delete_permission()`: Authenticated owner or org super-admin/admin
- Organization role checks: super-admin, admin, member

### Database Layer

**Models Used**:
- `User`: User accounts and authentication
- `Organization`: Organization metadata and quotas
- `UserOrganization`: Membership and roles
- `Repository`: Repository metadata (name, type, visibility)
- `File`: File tracking (size, SHA256, LFS status)
- `Commit`: Commit metadata (author, message, timestamp)
- `LFSObjectHistory`: LFS object version tracking
- `SSHKey`: SSH public keys for Git operations

**Transaction Patterns**:
- Atomic operations with database transactions
- Optimistic locking for concurrent updates
- Incremental storage counter updates
- Periodic recalculation for drift correction

### Storage Layer

**LakeFS Integration**:
- Git-like version control for all repositories
- Branch and commit management
- Object metadata and physical address resolution
- Diff calculation between revisions
- Tag support for pinning versions

**S3 Integration**:
- Object storage for all file content
- Presigned URLs for direct client-to-S3 transfers
- Content-addressable storage (SHA256-based keys)
- LFS object deduplication across repositories
- Balanced directory structure for LFS (`lfs/{oid[:2]}/{oid[2:4]}/{oid}`)

### Cross-Module Communication

**Common Patterns**:

1. **Quota Check Before Upload**:
   ```
   commit/operations.py → quota/util.check_quota()
   git/routers/lfs.py → quota/util.check_quota()
   ```

2. **Storage Update After Commit**:
   ```
   commit/operations.py → quota/util.update_namespace_storage()
   ```

3. **LFS Garbage Collection**:
   ```
   repo/routers/crud.py → storage_cleanup.record_repository_lfs()
   commit/operations.py → lfs_gc.record_evicted_versions()
   (deletion runs in the storage.collect_lfs background task, see lfs_gc.py)
   ```

4. **HuggingFace Error Responses**:
   ```
   All routers → repo/utils/hf.hf_repo_not_found()
   All routers → repo/utils/hf.hf_server_error()
   ```

## API Design Principles

### RESTful Architecture

- Resource-based URLs (`/repos`, `/commits`, `/quota`)
- Standard HTTP methods (GET, POST, PUT, DELETE)
- Proper HTTP status codes (200, 404, 403, 413, 500)
- JSON request/response bodies
- Query parameters for filtering and pagination

### HuggingFace Hub Compatibility

- Endpoint path compatibility (`/{repo_type}s/{namespace}/{name}`)
- Response format matching (JSON structure, field names)
- Error code compatibility (HFErrorCode enumeration)
- Header compatibility (`X-Repo-Commit`, `X-Linked-Etag`)
- Client library support (huggingface_hub Python package)

### Common Patterns

**Authentication**:
```python
user: User = Depends(get_current_user)  # Required auth
user: User | None = Depends(get_optional_user)  # Optional auth
```

**Permission Checking**:
```python
check_repo_read_permission(repo_row, user)
check_repo_write_permission(repo_row, user)
check_repo_delete_permission(repo_row, user)
```

**Error Handling**:
```python
if not repo:
    return hf_repo_not_found(repo_id, repo_type)

try:
    # Operation
except Exception as e:
    return hf_server_error(f"Operation failed: {str(e)}")
```

**Quota Enforcement**:
```python
allowed, error_msg = check_quota(namespace, bytes, is_private, is_org)
if not allowed:
    raise HTTPException(413, detail={"error": error_msg})
```

## Configuration

**Key Configuration Parameters** (`config.py`):

- `cfg.app.api_base`: API base path (default: `/api`)
- `cfg.app.lfs_threshold_bytes`: LFS size threshold
- `cfg.app.debug_log_payloads`: Debug logging for request/response
- `cfg.lakefs.*`: LakeFS connection settings
- `cfg.s3.*`: S3 storage settings
- `cfg.default_user_private_quota_bytes`: Default user private quota
- `cfg.default_user_public_quota_bytes`: Default user public quota
- `cfg.default_org_private_quota_bytes`: Default org private quota
- `cfg.default_org_public_quota_bytes`: Default org public quota

## Error Handling Strategy

### HTTP Status Codes

- `200 OK`: Successful operation
- `302 Found`: Redirect to presigned S3 URL
- `400 Bad Request`: Invalid input or operation not allowed
- `403 Forbidden`: Permission denied
- `404 Not Found`: Repository, file, or resource not found
- `409 Conflict`: Operation conflict (e.g., branch already exists, merge conflict)
- `413 Payload Too Large`: Quota exceeded
- `500 Internal Server Error`: Unexpected server error

### HuggingFace Error Codes

Implemented in `repo/utils/hf.py`:

- `RepoNotFound`: Repository does not exist
- `RevisionNotFound`: Branch or commit not found
- `EntryNotFound`: File or path not found
- `BadRequest`: Invalid request parameters
- `Unauthorized`: Authentication required
- `Forbidden`: Permission denied

### Error Response Format

```json
{
  "error": "Human-readable error message",
  "detail": "Additional error details (optional)"
}
```

## Performance Considerations

### Optimization Strategies

1. **Parallel Processing**:
   - Concurrent file processing in commits (`asyncio.gather`)
   - Parallel storage calculation for multiple repositories
   - Parallel diff entry processing

2. **Caching**:
   - Presigned URL caching (15-minute default)
   - Database query optimization with proper indexes
   - LakeFS API response caching

3. **Streaming**:
   - Git pack file streaming via side-band protocol
   - Large file uploads direct to S3 (bypass application)
   - Presigned URLs for downloads (direct S3 access)

4. **Incremental Updates**:
   - Storage counter updates (avoid full recalculation)
   - File deduplication checks (skip unchanged files)
   - Garbage collection only when needed

5. **Pagination**:
   - Commit history pagination
   - Repository listing pagination
   - LakeFS object listing pagination (1000 per request)

## Development Guidelines

### Adding New Endpoints

1. Create router in appropriate module
2. Define Pydantic models for request/response
3. Implement authentication and permission checks
4. Add business logic with proper error handling
5. Update storage quotas if modifying data
6. Add logging for debugging
7. Register router in `main.py`

### Testing Checklist

- Authentication and authorization scenarios
- Permission checks for different user roles
- Quota enforcement for uploads
- Error handling and edge cases
- HuggingFace client compatibility
- Git client compatibility (for git/ module)
- Database transaction integrity
- LakeFS error scenarios

### Logging

All modules use structured logging:

```python
from kohakuhub.logger import get_logger
logger = get_logger("MODULE_NAME")

logger.info("Operation successful")
logger.warning("Potential issue detected")
logger.error("Operation failed")
logger.exception("Exception details", exception_obj)
```

## Future Enhancements

### Planned Features

1. **Git Protocol**:
   - Full push implementation with LakeFS integration
   - SSH protocol support (beyond HTTP)
   - Shallow clone and partial clone support
   - Pack file caching and delta compression

2. **Repository Management**:
   - Repository templates
   - Repository forking
   - Repository archiving
   - Webhook notifications

3. **Collaboration**:
   - Pull request system
   - Code review workflows
   - Discussion threads
   - Access tokens with fine-grained permissions

4. **Storage**:
   - Multipart upload for files >5GB
   - Background garbage collection jobs
   - Storage analytics and insights
   - Quota grace periods

5. **Performance**:
   - Redis caching layer
   - Background task queue (Celery)
   - CDN integration for popular files
   - Read replicas for database

## References

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [LakeFS API Documentation](https://docs.lakefs.io/)
- [HuggingFace Hub API](https://huggingface.co/docs/hub/api)
- [Git HTTP Transfer Protocols](https://git-scm.com/docs/http-protocol)
- [Git LFS Specification](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md)
- [S3 Presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/PresignedUrlUploadObject.html)
