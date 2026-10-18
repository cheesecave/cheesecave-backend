---
title: Configuration Reference
description: All environment variables and settings for KohakuHub.
icon: i-carbon-settings-adjust
---

# Configuration Reference

KohakuHub can be configured via a `config.toml` file or by setting environment variables. Environment variables will always override values from a `config.toml` file.

By default, the application looks for `config.toml` in the current working directory. You can specify a different path using the `HUB_CONFIG` environment variable.

## Variable names

Every setting below is read from `CHEESE_CAVE_<NAME>`. The older `KOHAKU_HUB_<NAME>` is still accepted as a fallback, so a deployment written before the rename keeps working unchanged. When both are set for the same setting, `CHEESE_CAVE_<NAME>` wins and the `KOHAKU_HUB_<NAME>` value is ignored; an empty value counts as set. At startup the application logs, by name only, the variables that still use the old prefix and any that are set under both, so you can rename them when convenient.

Two things keep their older spelling on purpose: the LakeFS credentials file the containers share (`credentials.env`) is written with `KOHAKU_HUB_LAKEFS_ACCESS_KEY` and `KOHAKU_HUB_LAKEFS_SECRET_KEY`, because older images and shell scripts read it (both spellings are read); and an image build accepts `KOHAKU_HUB_GIT_SHA` and `KOHAKU_HUB_BUILD_TIME` besides the `CHEESE_CAVE_` build arguments.

In Compose, the shipped files read `CHEESE_CAVE_<NAME>` from `.env` first and `KOHAKU_HUB_<NAME>` second, so an existing `.env` needs no change. The three secrets Compose used to require (`SESSION_SECRET`, `ADMIN_SECRET_TOKEN`, `DATABASE_KEY`) are now checked by the container's entrypoint: with neither spelling in `.env` the container exits at start with a message naming both, instead of `docker compose up` failing. A Compose override file that sets a `KOHAKU_HUB_<NAME>` variable for a container loses to the `CHEESE_CAVE_<NAME>` the shipped file sets; rename it in the override.

## Application Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_BASE_URL` | The base URL of the application, used for generating links. | `http://localhost:48888` |
| `CHEESE_CAVE_API_BASE` | The base path for the API. | `/api` |
| `CHEESE_CAVE_SITE_NAME` | The name of the site, displayed in the UI. | `KohakuHub` |
| `CHEESE_CAVE_DEBUG_LOG_PAYLOADS`| If `true`, logs request and response payloads for debugging. | `false` |
| `CHEESE_CAVE_REPOSITORY_REVERT_ENABLED` | Enables Revert, effective only when `db_backend` is exactly `postgres`. Set `false` to switch it off; see [Defaults](../api/branches.md#defaults). | `true` |
| `CHEESE_CAVE_REPOSITORY_RESET_ENABLED` | Enables Reset, effective only when `db_backend` is exactly `postgres` and LakeFS is 1.48.1 or later ([LakeFS compatibility](../deployment/lakefs.md)). Set `false` to switch it off; see [Defaults](../api/branches.md#defaults). | `true` |
| `CHEESE_CAVE_COMMIT_ID_FORMAT` | The form of a commit id in every response: `hub` is the 40 hex of the Hugging Face Hub, which `huggingface_hub` accepts as a `parent_commit` and names its cache folders by; `full` is the 64 hex LakeFS has, a **transitional** form for downstream systems that stored them (`huggingface_hub` refuses 64 hex as a `parent_commit`, so `datasets.push_to_hub` and chained `create_commit` calls fail with it). Requests take both forms whatever this says; see [Commit ids](../api/commits.md#commit-ids). | `hub` |
| `CHEESE_CAVE_REPOSITORY_SQUASH_ENABLED` | Enables Super Squash, effective only when `db_backend` is exactly `postgres`. Set `false` to switch it off; see [Defaults](../api/branches.md#defaults). | `true` |

## Database Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_DB_BACKEND` | The database backend to use. Can be `sqlite` or `postgres`. | `sqlite` |
| `CHEESE_CAVE_DATABASE_URL` | The connection URL for the database. | `sqlite:///./hub.db` |

## S3 Storage Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_S3_PUBLIC_ENDPOINT` | The address clients reach the S3 service at: every presigned URL (download, upload, multipart part) is **signed for this address** and points to it. It is the same service as `CHEESE_CAVE_S3_ENDPOINT` under another name (the compose file's `minio:9000` inside, `127.0.0.1:29001` outside). A proxy in front of it must pass the `Host` header through unchanged (SigV4 signs it); a path on this address that the endpoint does not have is the proxy's mount point and is removed again by the proxy. For R2 and AWS S3 set it to the endpoint itself (Cloudflare documents R2 presigned URLs for the S3 API domain only, not for a custom domain). | `http://localhost:9000` |
| `CHEESE_CAVE_S3_ENDPOINT` | The internal S3 endpoint for the application. A path after the host names the real bucket: with `https://<account>.r2.cloudflarestorage.com/<bucket>`, `CHEESE_CAVE_S3_BUCKET` is a key prefix inside `<bucket>`, after any further path. The first path segment is always taken as the bucket, so an S3 service mounted under a sub-path of a proxy is not supported. LakeFS's `blockstore.s3.endpoint` must be the same, and `CHEESE_CAVE_S3_PUBLIC_ENDPOINT` must end with the same bucket. | `http://localhost:9000` |
| `CHEESE_CAVE_S3_ACCESS_KEY` | The access key for the S3 bucket. | `test-access-key` |
| `CHEESE_CAVE_S3_SECRET_KEY` | The secret key for the S3 bucket. | `test-secret-key` |
| `CHEESE_CAVE_S3_BUCKET` | The name of the S3 bucket. | `test-bucket` |
| `CHEESE_CAVE_S3_REGION` | The S3 region. | `us-east-1` |
| `CHEESE_CAVE_S3_SIGNATURE_VERSION` | The S3 signature version (e.g., `s3v4` for AWS S3/R2). | `None` |

## LakeFS Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_LAKEFS_ENDPOINT` | The endpoint for the LakeFS server. | `http://localhost:8000` |
| `CHEESE_CAVE_LAKEFS_ACCESS_KEY` | The access key for LakeFS. | `test-access-key` |
| `CHEESE_CAVE_LAKEFS_SECRET_KEY` | The secret key for LakeFS. | `test-secret-key` |
| `CHEESE_CAVE_LAKEFS_REPO_NAMESPACE` | The default namespace for repositories in LakeFS. | `hf` |
| `CHEESE_CAVE_LAKEFS_OPERATION_CONCURRENCY` | Concurrent LakeFS reads one branch operation (reset, revert, merge) makes while recording the regular files it changed. Around 8 saturates LakeFS; more only slows other requests down. | `8` |

## Git LFS Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_LFS_THRESHOLD_BYTES` | The file size threshold in bytes to trigger LFS. | `5242880` (5MB) |
| `CHEESE_CAVE_LFS_MULTIPART_THRESHOLD_BYTES` | The threshold for using multipart uploads for LFS. | `104857600` (100MB) |
| `CHEESE_CAVE_LFS_MULTIPART_CHUNK_SIZE_BYTES` | The chunk size for LFS multipart uploads. | `52428800` (50MB) |
| `CHEESE_CAVE_LFS_KEEP_VERSIONS` | The number of LFS file versions to keep during garbage collection. | `5` |
| `CHEESE_CAVE_LFS_AUTO_GC` | If `true`, LFS versions beyond the keep count are collected in the background. | `false` |
| `CHEESE_CAVE_USAGE_RECOUNT_INTERVAL_HOURS` | Repeats the full storage usage recount (`usage.recount`) every this many hours. Usage is kept up to date as repositories change; this is a safety net. `0` turns it off. | `0` |

## Authentication & Session Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_REQUIRE_EMAIL_VERIFICATION` | If `true`, requires email verification for new users. | `false` |
| `CHEESE_CAVE_INVITATION_ONLY` | If `true`, disables public registration and requires an invitation. | `false` |
| `CHEESE_CAVE_SESSION_SECRET` | The secret key for session management. **CHANGE IN PRODUCTION!** | `change-me-in-production` |
| `CHEESE_CAVE_SESSION_EXPIRE_HOURS` | How long a login session lasts after its last use, in hours. Using a session renews it (and its cookie), so someone who comes back within this window stays signed in. | `720` (30 days) |
| `CHEESE_CAVE_SESSION_ABSOLUTE_DAYS` | The longest a session lives from the login, in days, however often it is used. `0` means no limit. | `365` |
| `CHEESE_CAVE_TOKEN_EXPIRE_DAYS` | The API token expiration time in days. | `365` |

## Admin API Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_ADMIN_ENABLED` | If `true`, enables the admin API. | `true` |
| `CHEESE_CAVE_ADMIN_SECRET_TOKEN` | The secret token for accessing the admin API. **CHANGE IN PRODUCTION!** With the default value, or fewer than `CHEESE_CAVE_ADMIN_MIN_TOKEN_LENGTH` characters, the admin API is closed (`503`). `scripts/generate_secret.py` makes a good one. | `change-me-in-production` |
| `CHEESE_CAVE_ADMIN_MIN_TOKEN_LENGTH` | The shortest admin token the admin API accepts to run with. | `32` |
| `CHEESE_CAVE_ADMIN_ALLOW_WEAK_TOKEN` | If `true`, a short or default admin token is allowed. For development and tests only. | `false` |
| `CHEESE_CAVE_ADMIN_AUTH_THROTTLE_ENABLED` | If `true`, a client that keeps sending wrong admin tokens has to wait (see below). | `true` |
| `CHEESE_CAVE_ADMIN_AUTH_FREE_FAILURES` | Consecutive wrong tokens per client that cost nothing. | `5` |
| `CHEESE_CAVE_ADMIN_AUTH_BASE_WAIT_SECONDS` | From the next failure on, the client has to wait this long; each further failure doubles the wait. | `1` |
| `CHEESE_CAVE_ADMIN_AUTH_MAX_WAIT_SECONDS` | The longest wait. | `900` |
| `CHEESE_CAVE_ADMIN_AUTH_RESET_SECONDS` | A client's count starts again after this long without a failure. A correct token resets it at once. | `1800` |
| `CHEESE_CAVE_ADMIN_AUTH_ALERT_PER_MINUTE` | Log one warning a minute when more than this many wrong tokens arrive in that minute, from any clients. `0` turns it off. | `50` |
| `CHEESE_CAVE_ADMIN_AUTH_TRUSTED_PROXY_HOPS` | How many proxies of yours stand in front of the API. `0` ignores `X-Forwarded-For`; `N` takes the N-th address from the right. | `0` |
| `CHEESE_CAVE_ADMIN_AUTH_ALLOWLIST` | Comma-separated addresses or networks that are never counted or made to wait. | empty |

### Wrong admin tokens

Wrong tokens are counted per client address. The first few cost nothing; after that every further failure starts a wait of 1 s, 2 s, 4 s ... up to 15 minutes, during which the client gets `429` with a `Retry-After` header (the token is not even compared, and asking again does not extend the wait). A correct token, or 30 minutes without a failure, resets the client. There is no global lock: another client is never affected.

Behind a reverse proxy the API sees the proxy's address, so set `CHEESE_CAVE_ADMIN_AUTH_TRUSTED_PROXY_HOPS` to the number of proxies in front of it (1 for the shipped nginx). With `0` and an `X-Forwarded-For` header present, clients cannot be told apart and nobody is made to wait; a warning in the log says so. `GET /admin/api/auth/client` (with a valid token) shows the client address the server computed, so you can compare it with your own. The counters live in each worker process, so with several workers the allowance is up to that many times larger.

## Storage Quota Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_DEFAULT_USER_PRIVATE_QUOTA_BYTES` | The default private repo quota for users in bytes. | `None` (unlimited) |
| `CHEESE_CAVE_DEFAULT_USER_PUBLIC_QUOTA_BYTES` | The default public repo quota for users in bytes. | `None` (unlimited) |
| `CHEESE_CAVE_DEFAULT_ORG_PRIVATE_QUOTA_BYTES` | The default private repo quota for organizations in bytes. | `None` (unlimited) |
| `CHEESE_CAVE_DEFAULT_ORG_PUBLIC_QUOTA_BYTES` | The default public repo quota for organizations in bytes. | `None` (unlimited) |

## Fallback Source Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_FALLBACK_ENABLED` | If `true`, enables fallback to external sources. | `true` |
| `CHEESE_CAVE_FALLBACK_CACHE_TTL` | The cache TTL for fallback repo mappings in seconds. | `300` |
| `CHEESE_CAVE_FALLBACK_TIMEOUT` | The HTTP request timeout for external sources in seconds. | `10` |
| `CHEESE_CAVE_FALLBACK_MAX_CONCURRENT` | The max concurrent requests to external sources (read into the config but not enforced yet). | `5` |
| `CHEESE_CAVE_FALLBACK_SOURCES` | A JSON list of global fallback sources. | `[]` |

## Background Task Worker Settings

Used by `khub-worker` (`python -m kohakuhub.worker`). See [Background Tasks](../development/background-tasks.md).

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_WORKER_CONCURRENCY` | Tasks run at once per worker process. | `4` |
| `CHEESE_CAVE_WORKER_LEASE_SECONDS` | A task is reclaimed by another worker if its lease is not renewed for this long. | `60` |
| `CHEESE_CAVE_WORKER_POLL_INTERVAL_SECONDS` | Idle wait between claim attempts. | `1.0` |
| `CHEESE_CAVE_WORKER_SHUTDOWN_GRACE_SECONDS` | Drain time on SIGTERM before running tasks are cancelled. Also how long a task whose cancellation was requested may take to stop on its own before it is interrupted. | `30` |
| `CHEESE_CAVE_WORKER_FLUSH_INTERVAL_SECONDS` | How often a running task's progress and logs are stored and a cancellation request is noticed. The lease is renewed at least every `lease_seconds / 3` regardless. | `5.0` |
| `CHEESE_CAVE_WORKER_LOG_MAX_BYTES_PER_ATTEMPT` | Cap on the log captured for one attempt of a task. Records past it are dropped after a truncation notice. | `10485760` |
| `CHEESE_CAVE_WORKER_SUCCEEDED_RETENTION_DAYS` | Succeeded tasks are deleted after this many days. | `7` |
| `CHEESE_CAVE_WORKER_FAILED_RETENTION_DAYS` | Failed and cancelled tasks are deleted after this many days. | `30` |
| `CHEESE_CAVE_WORKER_NAME` | Optional prefix for the worker name shown in the admin panel (`<name>-<hostname>`). Empty uses the hostname alone, which already tells replicas apart. | `""` |
| `CHEESE_CAVE_WORKER_QUEUES` | Comma-separated queues this worker consumes; empty consumes all. | `""` |

## SMTP (Email) Settings

| Variable | Description | Default |
| --- | --- | --- |
| `CHEESE_CAVE_SMTP_ENABLED` | If `true`, enables SMTP for sending emails. | `false` |
| `CHEESE_CAVE_SMTP_HOST` | The SMTP server host. | `localhost` |
| `CHEESE_CAVE_SMTP_PORT` | The SMTP server port. | `587` |
| `CHEESE_CAVE_SMTP_USERNAME` | The username for SMTP authentication. | `""` |
| `CHEESE_CAVE_SMTP_PASSWORD` | The password for SMTP authentication. | `""` |
| `CHEESE_CAVE_SMTP_FROM` | The "from" email address for outgoing emails. | `noreply@localhost` |
| `CHEESE_CAVE_SMTP_TLS` | If `true`, uses TLS for SMTP connections. | `true` |
