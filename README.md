# CheeseCave Backend

English | [简体中文](README.zh-CN.md)

CheeseCave is a self-hosted model and dataset repository service compatible with
Hugging Face clients. It is an independent fork of KohakuHub, split into three
repositories that can be developed and updated separately.

| Repository | Responsibility |
| --- | --- |
| [cheesecave-backend](https://github.com/cheesecave/cheesecave-backend) | API, Git/LFS, background jobs, storage, database migrations and shared Compose configuration |
| [cheesecave-web](https://github.com/cheesecave/cheesecave-web) | Main website, repository browsing, file and dataset previews |
| [cheesecave-admin](https://github.com/cheesecave/cheesecave-admin) | Administration, users, quotas and runtime configuration |

This repository contains the FastAPI backend, worker and infrastructure
configuration. Both frontends build their own static images and can be updated
independently. The backend does not require frontend source or local `dist`
directories.

## Local development

Use Python 3.10+, Docker and Bash. On Windows, run Make commands through WSL or
Git Bash.

```sh
git clone https://github.com/cheesecave/cheesecave-backend.git
cd cheesecave-backend
python -m venv venv
# Activate the virtual environment for your shell, then install dependencies.
pip install -e ".[dev]"
make init-env
# Edit the ignored .env.dev for your environment.
make infra-up
make backend
```

The API listens at `http://localhost:48888` by default. Once it has initialized
tables and credentials, run `make worker` in another terminal. Start frontend
development servers from their own repositories.

```sh
make test
# Test a specific backend module:
make test-backend RANGE_DIR=api/repo/routers
```

The full backend suite requires PostgreSQL, MinIO and LakeFS services and keeps
its 80% coverage gate. See [local development](docs/development/local-dev.md).
Inherited GitHub Actions and Codecov configuration were removed before splitting;
local test and build commands remain available.

## Deploy the full project from source

Place the three repositories in the same parent directory:

```text
CheeseCave/
  cheesecave-backend/
  cheesecave-web/
  cheesecave-admin/
```

From the backend directory:

```sh
python scripts/generate_docker_compose.py --generate-config
# Edit .env: public URLs, credentials, UID/GID and image references.
docker compose -f compose.yml -f compose.build.yml config
docker compose -f compose.yml -f compose.build.yml up -d --build
```

The configuration helper prepares configuration and random credentials without
starting services. See [deployment](docs/deployment/docker.md) for persistent
directory permissions and preparations when migrating an existing installation.
The gateway defaults to port `28080`, with the website at `/` and Admin at
`/admin/`. API and worker must use the same backend image.

For deployment using existing images, set `CHEESECAVE_BACKEND_IMAGE`,
`CHEESECAVE_WEB_IMAGE` and `CHEESECAVE_ADMIN_IMAGE` independently in `.env`, then
run `docker compose up -d`. The default `:local` references are for source builds;
they do not imply that public release images have been published.

## Independent updates and compatibility

To update only the website, change its image reference in `.env`, then run:

```sh
docker compose pull hub-web
docker compose up -d --no-deps hub-web
```

For Admin, replace the service name with `hub-admin`. For sibling source builds,
use `docker compose -f compose.yml -f compose.build.yml up -d --build --no-deps hub-web`,
or the equivalent command for Admin. The gateway re-resolves container addresses
after replacement. Update `hub-api` and `khub-worker` together for backend
changes, checking migration and background-job compatibility. See the deployment
guide for rollback instructions.

The project and distribution are named CheeseCave, while Python imports remain
`kohakuhub`. Existing `KOHAKU_HUB_*` settings, protocol identities, migrations,
storage identifiers and client paths are retained. CheeseCave is the default
display name; administrator-configured branding takes precedence. New frontend
features require supporting backend APIs, and incompatible changes must be
documented explicitly.

## Origin and licenses

CheeseCave derives from [deepghs/KohakuHub](https://github.com/deepghs/KohakuHub),
based on [KohakuBlueleaf/KohakuHub](https://github.com/KohakuBlueleaf/KohakuHub).
Original attribution and copyright notices for KohakuBlueLeaf, DeepGHS and other
contributors are retained. CheeseCave is an independent fork.

The backend retains the complete original remote Git history, followed by new
commits for CI removal, splitting and subsequent work. Both frontends start with
fresh history and an initial commit titled "从原仓库分叉".
See [source provenance](provenance/UPSTREAM.md), the
[original README](provenance/README.upstream.md) and
[original changelog](provenance/CHANGELOG.upstream.md).

[LICENSE](LICENSE) and [LICENSING.md](LICENSING.md) retain their original text.
The Dataset Viewer backend retains its separate
[license](src/kohakuhub/datasetviewer/LICENSE). Follow the license applicable to
each component. Historical monorepo paths in the original licensing guide remain
as source information; the links above identify the current locations.

See [dated notices](NOTICE.md) for attribution and modification notices.

Categorized [manual regression CI](docs/development/ci.md) is prepared for later runs, with [中文说明](docs/development/ci.zh-CN.md). Preparing this configuration does not enable or trigger GitHub Actions.
