# CheeseCave deployment with independent images

Run Compose from **cheesecave-backend**. The backend owns the gateway, PostgreSQL,
LakeFS, MinIO, Valkey, API and background worker deployment. Main web and admin
are separate images; neither frontend source nor dist directory is required by
the release deployment.

## Configure a fresh installation

```sh
python scripts/generate_docker_compose.py --generate-config
# Edit the ignored .env: public URLs, secrets and UID/GID.
docker compose config
```

The helper creates secrets without overwriting an existing file. It does not
start containers. `.env.compose.example` documents all release variables. The
former interactive monorepo generator and root pnpm deployment workflow are
retired. `scripts/deploy.py` prints the native Compose commands only.

`CHEESECAVE_BACKEND_IMAGE`, `CHEESECAVE_WEB_IMAGE`, and `CHEESECAVE_ADMIN_IMAGE`
accept full image references, including `registry/name@sha256:digest`. Unset,
they default to `ghcr.io/cheesecave/cheesecave-{backend,web,admin}:${CHEESECAVE_VERSION:-latest}`,
published by each repository's "Publish image" workflow (`latest` and `sha-*` from
`main`, semver tags from `v*` tags). The source-build overlay (`compose.build.yml`)
tags `cheesecave-*:local` instead. To run the published images:

```sh
docker compose pull
docker compose up -d
```

Provision the bind-mounted `hub-meta/lakefs-data` and `hub-meta/valkey-data`
directories with the UID/GID configured in `.env`. Existing data lives in the
same `hub-meta/` and `hub-storage/` paths. Use deployment-specific overrides
for external databases/storage rather than copying old generated configs.
The default LakeFS image is pinned to Apache-2.0 release 1.86.0. See
[lakefs.md](lakefs.md) and [production.md](production.md) for external R2 details.

The API runs existing migrations and initializes LakeFS credentials in
`hub-meta/hub-api`. Worker replicas use the **same backend image and environment**,
read those credentials through a read-only mount, and wait for the API's tables.
Do not launch a different backend build as the worker against the same database.

## Optional source builds

Use three sibling checkouts:

```text
CheeseCave/
  cheesecave-backend/
  cheesecave-web/
  cheesecave-admin/
```

```sh
# Optional build identity; /api/version still uses its compatibility API identifier.
export KOHAKU_HUB_GIT_SHA="$(git rev-parse HEAD)"
export KOHAKU_HUB_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export CHEESECAVE_WEB_GIT_SHA="$(git -C ../cheesecave-web rev-parse HEAD)"
export CHEESECAVE_ADMIN_GIT_SHA="$(git -C ../cheesecave-admin rev-parse HEAD)"
export CHEESECAVE_WEB_GIT_DIRTY=false
export CHEESECAVE_ADMIN_GIT_DIRTY=false
# Set a UI's DIRTY value to true if its `git status --porcelain` is nonempty.
docker compose -f compose.yml -f compose.build.yml build
docker compose -f compose.yml -f compose.build.yml up -d
```

These opt-in build arguments identify the backend through `/api/version` and
the separate UI builds through their footers. Blank SHA values mean unknown;
set dirty markers accurately for worktree builds. Source identity is distinct
from the pinned image digest. Supply each checkout's commit SHA explicitly
when building images; no inherited CI pipeline remains enabled.

The API and worker build from this checkout, web/admin from their siblings. The
base Compose needs no sibling checkout once release images exist.

## Independent updates and rollback

After changing only `CHEESECAVE_WEB_IMAGE` in `.env`:

```sh
docker compose pull hub-web
docker compose up -d --no-deps hub-web
```

For admin, use `hub-admin` instead. These commands recreate only that frontend;
API, worker, infrastructure and the other UI retain their images. The gateway
uses Docker DNS with a five-second validity and variable upstream addresses,
so it picks up a recreated container's IP without a gateway restart. Keep
`/admin/` in the admin image's served paths; the gateway does not strip it.

Rollback a UI by restoring its previous pinned image reference and repeating
the same `up -d --no-deps` command. For backend updates, update **both** `hub-api`
and `khub-worker` together; check migration and job compatibility before any
backend rollback. `docker-compose.example.yml` is a compatibility copy of the
maintained `compose.yml`, not a second deployment design.

## Routes and persistent identities

The public gateway keeps `/` for web, `/admin/` for admin, and `/admin/api/`,
`/api/`, `/org/`, Git smart HTTP, Git LFS, and typed/legacy resolve routes for
the backend. Cookies and bearer authentication pass through the same origin.

Project/repository names change to CheeseCave. Python imports remain
`kohakuhub`; `KOHAKU_HUB_*`, `HUB_CONFIG`, database/table names, migration
numbers, LakeFS repository identifiers and object key prefixes stay compatible.
Renaming a checkout is not a data migration. Preserve configured credentials
and historical storage layout during an existing installation's cutover.
