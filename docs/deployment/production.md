---
title: Production Deployment
description: SSL, domain setup, external S3, security hardening
icon: i-carbon-cloud-upload
---

# Production Deployment

Deploy KohakuHub for production use.

## Component Versions

- **LakeFS 1.48.1 – 1.86.0** (not 1.70.0). The bundled docker compose pins
  `treeverse/lakefs:1.86.0`. If your production stack runs its own LakeFS,
  check it against [LakeFS Compatibility](lakefs.md) before rolling out
  KohakuHub. LakeFS 1.87.0 and later are licensed under the Business Source
  License 1.1, which limits production use to internal use; assess it before
  upgrading. The Admin portal's Health page shows the running version.

## SSL & Domain

**nginx config:**
```nginx
server {
    listen 443 ssl http2;
    server_name hub.yourdomain.com;
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # Forward every KohakuHub path to the backend. The hf_hub-compatible
    # public URLs (no /api prefix) MUST be reachable at the root since
    # huggingface_hub clients hit them directly:
    #   /<repo_type>s/<ns>/<name>/resolve/<rev>/<path>   (HEAD/GET file)
    #   /<repo_type>s/<ns>/<name>/tree/<rev>/...         (list files)
    #   /<ns>/<name>/resolve/...                         (model default)
    # The chain tester in the admin SPA exercises these same routes
    # (see ../cheesecave-admin/vite.config.js for the dev-mode mirror)
    # so misconfigured nginx → CHAIN_EXHAUSTED on every probe.
    location / {
        proxy_pass http://kohakuhub-backend:48888;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Admin SPA static bundle — path-prefixed under /admin so it
    # coexists with the hf_hub-compat root paths.
    location /admin/ {
        alias /var/www/kohakuhub-admin/;
        try_files $uri $uri/ /admin/index.html;
    }
}
```

**Update base URL:**
```yaml
KOHAKU_HUB_BASE_URL: https://hub.yourdomain.com
```

## External S3

**Cloudflare R2:**
```yaml
KOHAKU_HUB_S3_ENDPOINT: https://account.r2.cloudflarestorage.com
KOHAKU_HUB_S3_PUBLIC_ENDPOINT: https://account.r2.cloudflarestorage.com  # the same address
KOHAKU_HUB_S3_REGION: auto
KOHAKU_HUB_S3_SIGNATURE_VERSION: s3v4
```
Clients upload and download through presigned URLs, which are signed for `KOHAKU_HUB_S3_PUBLIC_ENDPOINT` (SigV4 signs the host). Cloudflare documents R2 presigned URLs for its S3 API domain, `<account>.r2.cloudflarestorage.com`, and says they cannot be used with custom domains; a `*.r2.dev` public-bucket URL is a development path (rate-limited). So the public endpoint is the endpoint itself.

**MinIO behind your own proxy (compose or not):** the endpoint is what the API reaches MinIO at (`http://minio:9000`), the public endpoint what clients reach it at (`https://s3.yourdomain.com`). The proxy must pass the `Host` header through unchanged (`proxy_set_header Host $http_host;` in nginx; its default sends the upstream's `minio:9000` instead, and the signature then no longer matches), must not buffer or limit uploads (`proxy_request_buffering off; client_max_body_size 0;`), and may mount MinIO under a path only if it removes that path again before the request reaches MinIO (the path is not part of the signature).

**An R2 bucket that holds other data too, or an API token scoped to one bucket:** put the bucket in the endpoint's path. `KOHAKU_HUB_S3_BUCKET` then names a key prefix inside it, and LakeFS must use the same endpoint:
```yaml
KOHAKU_HUB_S3_ENDPOINT: https://account.r2.cloudflarestorage.com/my-bucket
KOHAKU_HUB_S3_PUBLIC_ENDPOINT: https://account.r2.cloudflarestorage.com/my-bucket  # same bucket
KOHAKU_HUB_S3_BUCKET: hub-storage            # objects live under my-bucket/hub-storage/
LAKEFS_BLOCKSTORE_S3_ENDPOINT: https://account.r2.cloudflarestorage.com/my-bucket
LAKEFS_BLOCKSTORE_S3_DISCOVER_BUCKET_REGION: "false"  # its region probe of hub-storage can only fail
```
KohakuHub reaches the bucket at the endpoint's root, so copies, listings and bulk deletes stay inside the prefix. It needs no permission beyond that bucket. The first path segment is always the bucket: an S3 service mounted under a proxy's sub-path is not supported. Some LakeFS features do not work with this layout; KohakuHub uses none of them (see [LakeFS compatibility](lakefs.md#an-s3-endpoint-with-the-bucket-in-its-path)).

**AWS S3:**
```yaml
KOHAKU_HUB_S3_ENDPOINT: https://s3.amazonaws.com
KOHAKU_HUB_S3_REGION: us-east-1
KOHAKU_HUB_S3_FORCE_PATH_STYLE: false
```

## Security

**Change all secrets:**
```bash
python scripts/generate_secret.py
# Update SESSION_SECRET, ADMIN_SECRET_TOKEN
```

**Change passwords:**
- PostgreSQL
- MinIO
- LakeFS

## Scaling

**Multi-worker:**
```yaml
command: uvicorn kohakuhub.main:app --workers 8
```

Database uses `db.atomic()` for safety.

## Backups

```bash
docker exec postgres pg_dump -U hub kohakuhub | gzip > backup.sql.gz
```

## Rollback of the File key migration

Migration 033 (issue #11) makes the old unique key on `file (repository_id, path_in_repo)` non-unique, so
side branches can hold rows for paths `main` has. It runs at container start with the other
migrations, so a deploy applies it with no manual step. The api is down while its container is replaced
(production last measured about 9 s from container start to serving; in a rehearsal on a production-sized
database, 813k file rows, all migrations including 033 took about 1 s and the lock 033 holds 0.06 s); the
gateway answers 502 meanwhile. If 033 fails, the api container exits and stays down until restarted (restart
policy `no`): read its log, fix the cause (most often a long transaction holding `file`) and
restart.

Rolling back:

- The image can go back to any image that already writes the new key: `d3140a2` or later.
  Nothing in the database needs to change.
- It cannot go back to a pre-full-A image (`2f3f687` or earlier) after any branch has been
  created without restoring a database backup: those images need the old unique key, and the
  rows of other branches break it. Take a dump before deploying
  (`docker exec postgres pg_dump -U hub kohakuhub | gzip > before-033.sql.gz`) and restore it to go back.

To go back to `2f3f687` or earlier while no row of another branch exists (a branch made from a populated
`main` has rows), stop the api and the worker and run, in a container of the current image:

```bash
python scripts/db_migrations/032_file_branch_expand.py --rollback
```

It restores the old unique key and drops `file.branch`. With rows of other branches it refuses and
changes nothing; then restore the dump. Without either, such an image boots and reads, but every
commit to `main` fails (`no unique or exclusion constraint matching the ON CONFLICT specification`).
The `schema_rollout` table that 032 created is unused now; it is left in place.

See [Security](./security.md) for hardening guide.
