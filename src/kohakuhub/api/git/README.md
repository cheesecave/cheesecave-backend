# Git API Module

## Overview

The `kohakuhub.api.git` module provides comprehensive Git protocol support for KohakuHub, enabling native Git client operations (clone, fetch, pull, push) and Git Large File Storage (LFS) management. This module bridges Git clients with LakeFS-backed repositories, allowing seamless version control workflows while leveraging LakeFS's data lake capabilities.

## Purpose and Functionality

This module serves as the Git protocol layer for KohakuHub, implementing:

1. **Git Smart HTTP Protocol** - Full support for Git's Smart HTTP transport protocol, enabling standard Git operations through HTTP(S)
2. **Git LFS Integration** - Complete Git Large File Storage implementation with S3-backed storage and presigned URL generation
3. **SSH Key Management** - API endpoints for managing user SSH public keys with proper validation and fingerprinting
4. **LakeFS Bridge** - Pure Python implementation that bridges Git operations to LakeFS repositories without requiring native Git binaries

## Key Features

### Git Smart HTTP Protocol
- **Service Advertisement** - Implements `info/refs` endpoint for capability negotiation
- **Upload Pack** - Handles `git-upload-pack` for clone, fetch, and pull operations
- **Receive Pack** - Handles `git-receive-pack` for push operations
- **Pkt-line Protocol** - Full implementation of Git's packet-line framing format
- **Authentication** - Token-based authentication using Basic Auth with Git credentials

### Git LFS Support
- **Batch API** - Implements Git LFS Batch API specification for batch upload/download operations
- **S3 Direct Upload** - Generates presigned S3 URLs for direct client-to-S3 transfers (bypassing application server)
- **Deduplication** - SHA256-based content deduplication across repositories
- **Size Threshold** - Automatic LFS pointer creation for files exceeding configurable size threshold
- **LFS Verification** - Post-upload verification endpoint to confirm successful transfers
- **Browser Support** - Handles browser-specific upload requirements (Content-Type headers)
- **Quota Management** - Integrates with quota system to enforce storage limits

### SSH Key Management
- **Key Registration** - API endpoints for adding SSH public keys
- **Validation** - Cryptographic validation of SSH key formats using the `cryptography` library
- **Fingerprinting** - SHA256 fingerprint computation for key identification
- **Supported Formats** - ssh-rsa, ssh-dss, ecdsa-sha2-nistp{256,384,521}, ssh-ed25519
- **Key Lifecycle** - Create, read, list, and delete operations for SSH keys

### LakeFS Integration
- **Pure Python Implementation** - No dependencies on native Git binaries or pygit2
- **In-Memory Operations** - All Git object construction happens in memory without filesystem I/O
- **Dynamic Pack Generation** - Builds Git pack files on-demand from LakeFS object storage
- **LFS Pointer Generation** - Automatically creates LFS pointers for large files stored in LakeFS
- **Nested Tree Construction** - Builds proper Git tree hierarchies from flat file lists
- **Commit Synthesis** - Generates Git commit objects from LakeFS commit metadata

## Architecture

### Module Structure

```
kohakuhub/api/git/
├── __init__.py              # Module initialization
├── routers/                 # FastAPI route handlers
│   ├── __init__.py
│   ├── http.py             # Git Smart HTTP protocol endpoints
│   ├── lfs.py              # Git LFS Batch API endpoints
│   └── ssh_keys.py         # SSH key management endpoints
└── utils/                   # Core implementation utilities
    ├── __init__.py
    ├── server.py           # Git protocol handler utilities
    ├── lakefs_bridge.py    # Git-LakeFS bridge implementation
    └── objects.py          # Pure Python Git object construction
```

### Component Overview

#### Routers

**`routers/http.py`** - Git Smart HTTP Protocol
- `GET /{namespace}/{name}.git/info/refs` - Service advertisement (upload-pack/receive-pack)
- `POST /{namespace}/{name}.git/git-upload-pack` - Clone/fetch/pull operations
- `POST /{namespace}/{name}.git/git-receive-pack` - Push operations
- `GET /{namespace}/{name}.git/HEAD` - HEAD reference query

**`routers/lfs.py`** - Git LFS Batch API
- `POST /{namespace}/{name}.git/info/lfs/objects/batch` - LFS batch request handler (upload/download)
- `POST /api/{namespace}/{name}.git/info/lfs/verify` - LFS upload verification endpoint

**`routers/ssh_keys.py`** - SSH Key Management
- `GET /api/user/keys` - List user's SSH keys
- `POST /api/user/keys` - Add new SSH key
- `GET /api/user/keys/{key_id}` - Get SSH key details
- `DELETE /api/user/keys/{key_id}` - Remove SSH key

#### Utilities

**`utils/server.py`** - Git Protocol Handler
- Pkt-line encoding/decoding utilities (`pkt_line`, `parse_pkt_lines`)
- Git service info advertisement generation (`GitServiceInfo`)
- Upload-pack request handler (`GitUploadPackHandler`)
- Receive-pack request handler (`GitReceivePackHandler`)
- Git credential parsing for Basic Auth
- Side-band protocol support for multiplexed responses

**`utils/lakefs_bridge.py`** - Git-LakeFS Bridge
- `GitLakeFSBridge` - Main bridge class bridging Git operations to LakeFS
- Pure Python Git object construction (blobs, trees, commits)
- Pack file generation from LakeFS objects
- LFS pointer creation for large files
- `.gitattributes` generation (no `.lfsconfig`: git-lfs derives the endpoint from the clone URL)
- Pattern matching for LFS file detection
- Nested tree structure building from flat file lists

**`utils/objects.py`** - Git Object Construction
- Low-level Git object creation (blob, tree, commit)
- SHA-1 computation for Git objects
- Pack file format generation
- Pack object header encoding (variable-length format)
- Tree entry sorting (Git-compliant directory ordering)
- Nested tree builder for directory hierarchies

## Git Operations Flow

### Clone/Fetch/Pull Flow
1. Client requests `info/refs?service=git-upload-pack`
2. Server authenticates user and checks read permissions
3. Server queries LakeFS for refs and returns service advertisement
4. Client sends want/have negotiation via `git-upload-pack` request
5. `GitLakeFSBridge` builds pack file:
   - Lists all objects from LakeFS
   - Creates blob objects (or LFS pointers for large files)
   - Builds nested tree structure
   - Creates commit object with metadata
   - Assembles pack file with all objects
6. Server returns pack file via side-band protocol

### Push Flow (Partial Implementation)
1. Client requests `info/refs?service=git-receive-pack`
2. Server authenticates user and checks write permissions
3. Client sends ref updates and pack file via `git-receive-pack` request
4. Server parses ref updates and pack data
5. Server sends status report (currently accepts all pushes)
6. **Note**: Full push implementation with LakeFS integration is pending

### LFS Upload Flow
1. Client sends LFS batch request with operation="upload" and object list
2. Server authenticates user and checks write/quota permissions
3. For each object:
   - Check if already exists (SHA256 deduplication)
   - Generate presigned S3 upload URL with SHA256 checksum
   - Return upload action with URL and verify endpoint
4. Client uploads directly to S3 using presigned URL
5. Client calls verify endpoint to confirm upload completion
6. Server validates object exists in S3

### LFS Download Flow
1. Client sends LFS batch request with operation="download" and object list
2. Server authenticates user and checks read permissions
3. For each object:
   - Check if object exists in storage
   - Generate presigned S3 download URL
   - Return download action with URL
4. Client downloads directly from S3 using presigned URL

## Integration with KohakuHub

### LakeFS Integration
The module uses LakeFS as the underlying storage backend:
- Repositories in LakeFS map to KohakuHub repositories
- LakeFS branches map to Git branches
- LakeFS commits map to Git commits
- Git operations are translated to LakeFS API calls

### Authentication
- Uses KohakuHub's token-based authentication system
- Git HTTP operations accept Basic Auth with username and token
- Tokens are validated against the database and updated on use
- SSH keys are associated with user accounts for SSH Git operations

### Permissions
- Integrates with KohakuHub's permission system
- Read operations: `check_repo_read_permission()`
- Write operations: `check_repo_write_permission()`
- Supports public/private repository access control

### Storage
- LFS files stored in S3 with balanced directory structure (`lfs/{oid[:2]}/{oid[2:4]}/{oid}`)
- Presigned URL generation for direct client-S3 transfers
- Content-addressable storage using SHA256 hashing
- Deduplication at file level across all repositories

### Quota System
- LFS uploads checked against namespace quota limits
- Distinguishes between user and organization namespaces
- Considers repository privacy (public vs private) in quota calculations
- Returns appropriate error messages when quota exceeded

## Technical Implementation Details

### Pure Python Approach
The module is designed to be dependency-light and portable:
- No native Git binaries required
- No libgit2 or pygit2 dependencies
- All operations performed in-memory
- No temporary file creation
- Deterministic object generation for reproducibility

### Git Object Construction
Git objects are constructed following Git's exact specifications:
- Blob: `blob {size}\0{content}`
- Tree: `tree {size}\0{entries}` with proper sorting (directories treated as having trailing `/`)
- Commit: `commit {size}\0tree {sha}\nparent {sha}...\nauthor ...\ncommitter ...\n\n{message}`
- SHA-1 computed over complete object including header

### Pack File Format
Pack files follow Git's pack format v2:
- Header: `PACK` + version(4 bytes) + object_count(4 bytes)
- Objects: variable-length header + zlib-compressed content
- Footer: SHA-1 checksum of entire pack
- Object types: 1=commit, 2=tree, 3=blob
- Supports large packs via chunked side-band protocol

### LFS Pointer Format
LFS pointers follow the Git LFS specification:
```
version https://git-lfs.github.com/spec/v1
oid sha256:{sha256_hex}
size {size_in_bytes}
```

### Error Handling
- Comprehensive exception handling throughout
- Graceful degradation (empty packs when data unavailable)
- Detailed logging for debugging
- HTTP status codes follow Git and LFS specifications

## Dependencies

### External Libraries
- `fastapi` - Web framework for API endpoints
- `pydantic` - Request/response validation
- `cryptography` - SSH key validation and fingerprinting
- `asyncio` - Asynchronous I/O for concurrent operations

### Internal Dependencies
- `kohakuhub.config` - Application configuration
- `kohakuhub.db` - Database models and operations
- `kohakuhub.auth` - Authentication and authorization
- `kohakuhub.utils.lakefs` - LakeFS client utilities
- `kohakuhub.utils.s3` - S3 operations and presigned URLs
- `kohakuhub.api.quota` - Quota management utilities

## Future Enhancements

### Planned Features
- Full push implementation with LakeFS integration
- Multipart upload support for files >5GB
- Delta compression for pack files
- Pack file caching for improved performance
- SSH protocol support (in addition to HTTP)
- Webhook notifications for Git operations
- Advanced ref management (tags, branches beyond main)
- Shallow clone support
- Partial clone and sparse checkout support

### Performance Optimizations
- Pack file reuse and caching
- Incremental pack generation
- Object delta compression
- Parallel object retrieval from LakeFS
- Pack index file generation

## Development Notes

### Testing
When testing Git operations:
- Use actual Git clients (git command-line, GitHub Desktop, etc.)
- Test with repositories of various sizes
- Verify LFS threshold behavior
- Test quota enforcement
- Validate SSH key fingerprinting with known keys

### Debugging
- Set `cfg.app.debug_log_payloads = True` to log request/response payloads
- Check logs for detailed Git protocol exchanges
- Verify LakeFS API responses
- Monitor S3 presigned URL generation and usage
- Track pack file sizes and object counts

### Common Issues
- **Empty pack files**: Usually indicates LakeFS connection issues or empty repositories
- **LFS upload failures**: Check S3 permissions and presigned URL configuration
- **SSH key validation failures**: Verify key format matches supported types
- **Push rejection**: Full push support pending - currently returns success without updating LakeFS

## References

- [Git HTTP Transfer Protocols](https://git-scm.com/docs/http-protocol)
- [Git Pack Format](https://git-scm.com/docs/pack-format)
- [Git LFS Batch API](https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md)
- [Git LFS Specification](https://github.com/git-lfs/git-lfs/blob/main/docs/spec.md)
- [SSH Public Key File Format](https://datatracker.ietf.org/doc/html/rfc4253#section-6.6)
