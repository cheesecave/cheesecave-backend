---
title: Configuration Reference
description: All environment variables and settings for KohakuHub.
icon: i-carbon-settings-adjust
---

# Configuration Reference

KohakuHub can be configured via a `config.toml` file or by setting environment variables. Environment variables will always override values from a `config.toml` file.

By default, the application looks for `config.toml` in the current working directory. You can specify a different path using the `HUB_CONFIG` environment variable.

## Application Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_BASE_URL` | The base URL of the application, used for generating links. | `http://localhost:48888` |
| `KOHAKU_HUB_API_BASE` | The base path for the API. | `/api` |
| `KOHAKU_HUB_SITE_NAME` | The name of the site, displayed in the UI. | `KohakuHub` |
| `KOHAKU_HUB_DEBUG_LOG_PAYLOADS`| If `true`, logs request and response payloads for debugging. | `false` |
| `KOHAKU_HUB_REPOSITORY_REVERT_ENABLED` | Enables Revert only when `db_backend` is exactly `postgres`. Disabled by default until its integrity, recovery and canary gates pass; see [Defaults and reopening plan](../api/branches.md#defaults-and-reopening-plan). | `false` |
| `KOHAKU_HUB_REPOSITORY_RESET_ENABLED` | Enables Reset only when `db_backend` is exactly `postgres`. Disabled by default until its integrity, recovery and canary gates pass; see [Defaults and reopening plan](../api/branches.md#defaults-and-reopening-plan). | `false` |
| `KOHAKU_HUB_REPOSITORY_SQUASH_ENABLED` | Enables Super Squash only when `db_backend` is exactly `postgres`. Disabled by default until its integrity, recovery and canary gates pass; see [Defaults and reopening plan](../api/branches.md#defaults-and-reopening-plan). | `false` |

## Database Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_DB_BACKEND` | The database backend to use. Can be `sqlite` or `postgres`. | `sqlite` |
| `KOHAKU_HUB_DATABASE_URL` | The connection URL for the database. | `sqlite:///./hub.db` |

## S3 Storage Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_S3_PUBLIC_ENDPOINT` | The public-facing S3 endpoint, used for client downloads. | `http://localhost:9000` |
| `KOHAKU_HUB_S3_ENDPOINT` | The internal S3 endpoint for the application. | `http://localhost:9000` |
| `KOHAKU_HUB_S3_ACCESS_KEY` | The access key for the S3 bucket. | `test-access-key` |
| `KOHAKU_HUB_S3_SECRET_KEY` | The secret key for the S3 bucket. | `test-secret-key` |
| `KOHAKU_HUB_S3_BUCKET` | The name of the S3 bucket. | `test-bucket` |
| `KOHAKU_HUB_S3_REGION` | The S3 region. | `us-east-1` |
| `KOHAKU_HUB_S3_SIGNATURE_VERSION` | The S3 signature version (e.g., `s3v4` for AWS S3/R2). | `None` |

## LakeFS Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_LAKEFS_ENDPOINT` | The endpoint for the LakeFS server. | `http://localhost:8000` |
| `KOHAKU_HUB_LAKEFS_ACCESS_KEY` | The access key for LakeFS. | `test-access-key` |
| `KOHAKU_HUB_LAKEFS_SECRET_KEY` | The secret key for LakeFS. | `test-secret-key` |
| `KOHAKU_HUB_LAKEFS_REPO_NAMESPACE` | The default namespace for repositories in LakeFS. | `hf` |
| `KOHAKU_HUB_LAKEFS_OPERATION_CONCURRENCY` | Concurrent LakeFS requests one branch operation (reset) makes. Around 8 saturates LakeFS; more only slows other requests down. | `8` |

## Git LFS Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_LFS_THRESHOLD_BYTES` | The file size threshold in bytes to trigger LFS. | `5242880` (5MB) |
| `KOHAKU_HUB_LFS_MULTIPART_THRESHOLD_BYTES` | The threshold for using multipart uploads for LFS. | `104857600` (100MB) |
| `KOHAKU_HUB_LFS_MULTIPART_CHUNK_SIZE_BYTES` | The chunk size for LFS multipart uploads. | `52428800` (50MB) |
| `KOHAKU_HUB_LFS_KEEP_VERSIONS` | The number of LFS file versions to keep during garbage collection. | `5` |
| `KOHAKU_HUB_LFS_AUTO_GC` | If `true`, LFS versions beyond the keep count are collected in the background. | `false` |
| `KOHAKU_HUB_USAGE_RECOUNT_INTERVAL_HOURS` | Repeats the full storage usage recount (`usage.recount`) every this many hours. Usage is kept up to date as repositories change; this is a safety net. `0` turns it off. | `0` |

## Authentication & Session Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_REQUIRE_EMAIL_VERIFICATION` | If `true`, requires email verification for new users. | `false` |
| `KOHAKU_HUB_INVITATION_ONLY` | If `true`, disables public registration and requires an invitation. | `false` |
| `KOHAKU_HUB_SESSION_SECRET` | The secret key for session management. **CHANGE IN PRODUCTION!** | `change-me-in-production` |
| `KOHAKU_HUB_SESSION_EXPIRE_HOURS` | The session cookie expiration time in hours. | `168` (7 days) |
| `KOHAKU_HUB_TOKEN_EXPIRE_DAYS` | The API token expiration time in days. | `365` |

## Admin API Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_ADMIN_ENABLED` | If `true`, enables the admin API. | `true` |
| `KOHAKU_HUB_ADMIN_SECRET_TOKEN` | The secret token for accessing the admin API. **CHANGE IN PRODUCTION!** | `change-me-in-production` |

## Storage Quota Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_DEFAULT_USER_PRIVATE_QUOTA_BYTES` | The default private repo quota for users in bytes. | `None` (unlimited) |
| `KOHAKU_HUB_DEFAULT_USER_PUBLIC_QUOTA_BYTES` | The default public repo quota for users in bytes. | `None` (unlimited) |
| `KOHAKU_HUB_DEFAULT_ORG_PRIVATE_QUOTA_BYTES` | The default private repo quota for organizations in bytes. | `None` (unlimited) |
| `KOHAKU_HUB_DEFAULT_ORG_PUBLIC_QUOTA_BYTES` | The default public repo quota for organizations in bytes. | `None` (unlimited) |

## Fallback Source Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_FALLBACK_ENABLED` | If `true`, enables fallback to external sources. | `true` |
| `KOHAKU_HUB_FALLBACK_CACHE_TTL` | The cache TTL for fallback repo mappings in seconds. | `300` |
| `KOHAKU_HUB_FALLBACK_TIMEOUT` | The HTTP request timeout for external sources in seconds. | `10` |
| `KOHAKU_HUB_FALLBACK_MAX_CONCURRENT` | The max concurrent requests to external sources. | `5` |
| `KOHAKU_HUB_FALLBACK_SOURCES` | A JSON list of global fallback sources. | `[]` |

## Background Task Worker Settings

Used by `khub-worker` (`python -m kohakuhub.worker`). See [Background Tasks](../development/background-tasks.md).

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_WORKER_CONCURRENCY` | Tasks run at once per worker process. | `4` |
| `KOHAKU_HUB_WORKER_LEASE_SECONDS` | A task is reclaimed by another worker if its lease is not renewed for this long. | `60` |
| `KOHAKU_HUB_WORKER_POLL_INTERVAL_SECONDS` | Idle wait between claim attempts. | `1.0` |
| `KOHAKU_HUB_WORKER_SHUTDOWN_GRACE_SECONDS` | Drain time on SIGTERM before running tasks are cancelled. Also how long a task whose cancellation was requested may take to stop on its own before it is interrupted. | `30` |
| `KOHAKU_HUB_WORKER_FLUSH_INTERVAL_SECONDS` | How often a running task's progress and logs are stored and a cancellation request is noticed. The lease is renewed at least every `lease_seconds / 3` regardless. | `5.0` |
| `KOHAKU_HUB_WORKER_LOG_MAX_BYTES_PER_ATTEMPT` | Cap on the log captured for one attempt of a task. Records past it are dropped after a truncation notice. | `10485760` |
| `KOHAKU_HUB_WORKER_SUCCEEDED_RETENTION_DAYS` | Succeeded tasks are deleted after this many days. | `7` |
| `KOHAKU_HUB_WORKER_FAILED_RETENTION_DAYS` | Failed and cancelled tasks are deleted after this many days. | `30` |
| `KOHAKU_HUB_WORKER_NAME` | Optional prefix for the worker name shown in the admin panel (`<name>-<hostname>`). Empty uses the hostname alone, which already tells replicas apart. | `""` |
| `KOHAKU_HUB_WORKER_QUEUES` | Comma-separated queues this worker consumes; empty consumes all. | `""` |

## SMTP (Email) Settings

| Variable | Description | Default |
| --- | --- | --- |
| `KOHAKU_HUB_SMTP_ENABLED` | If `true`, enables SMTP for sending emails. | `false` |
| `KOHAKU_HUB_SMTP_HOST` | The SMTP server host. | `localhost` |
| `KOHAKU_HUB_SMTP_PORT` | The SMTP server port. | `587` |
| `KOHAKU_HUB_SMTP_USERNAME` | The username for SMTP authentication. | `""` |
| `KOHAKU_HUB_SMTP_PASSWORD` | The password for SMTP authentication. | `""` |
| `KOHAKU_HUB_SMTP_FROM` | The "from" email address for outgoing emails. | `noreply@localhost` |
| `KOHAKU_HUB_SMTP_TLS` | If `true`, uses TLS for SMTP connections. | `true` |
