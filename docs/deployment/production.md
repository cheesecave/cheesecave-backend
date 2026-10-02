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
    # (see src/kohaku-hub-admin/vite.config.js for the dev-mode mirror)
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
KOHAKU_HUB_S3_PUBLIC_ENDPOINT: https://pub.r2.dev
KOHAKU_HUB_S3_REGION: auto
KOHAKU_HUB_S3_SIGNATURE_VERSION: s3v4
```

**An R2 bucket that holds other data too, or an API token scoped to one bucket:** put the bucket in the endpoint's path. `KOHAKU_HUB_S3_BUCKET` then names a key prefix inside it, and LakeFS must use the same endpoint:
```yaml
KOHAKU_HUB_S3_ENDPOINT: https://account.r2.cloudflarestorage.com/my-bucket
KOHAKU_HUB_S3_PUBLIC_ENDPOINT: https://account.r2.cloudflarestorage.com/my-bucket  # same bucket
KOHAKU_HUB_S3_BUCKET: hub-storage            # objects live under my-bucket/hub-storage/
LAKEFS_BLOCKSTORE_S3_ENDPOINT: https://account.r2.cloudflarestorage.com/my-bucket
```
KohakuHub reaches the bucket at the endpoint's root, so copies, listings and bulk deletes stay inside the prefix. It needs no permission beyond that bucket. The first path segment is always the bucket: an S3 service mounted under a proxy's sub-path is not supported.

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

See [Security](./security.md) for hardening guide.
