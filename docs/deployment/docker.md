---
title: Docker Deployment
description: Complete Docker Compose setup guide for KohakuHub.
icon: i-carbon-container-services
---

# Docker Deployment

This guide provides a complete walkthrough for deploying KohakuHub using Docker Compose.

## Quick Deploy (Recommended)

This is the easiest and fastest way to get KohakuHub running.

### 1. Clone the Repository

```bash
git clone https://github.com/KohakuBlueleaf/KohakuHub.git
cd KohakuHub
```

### 2. Configure and Deploy

First, run the interactive script to generate your `docker-compose.yml` file. This will guide you through setting up the database, storage, and other services.

```bash
python scripts/generate_docker_compose.py
```

Then, run the deployment script. This will automatically build the frontend applications and start all the Docker services.

```bash
python scripts/deploy.py
```

That's it! The application is now running.

## Step-by-Step Setup

If you need more control over the setup process, you can follow these manual steps.

### 1. Clone the Repository

```bash
git clone https://github.com/KohakuBlueleaf/KohakuHub.git
cd KohakuHub
```

### 2. Generate `docker-compose.yml`

Copy the example file and manually edit it to fit your environment.

```bash
cp docker-compose.example.yml docker-compose.yml
```

### 3. Build the Frontend

Before starting the services, you need to build the frontend applications:

```bash
pnpm install
pnpm run build
```

### 4. Start the Services

To start all services in detached mode, run:

```bash
docker-compose up -d --build
```

To have `GET /api/version` report which code the deployment runs (its `build.git_sha` and `build.build_time`), pass them when building:

```bash
docker-compose build \
  --build-arg KOHAKU_HUB_GIT_SHA=$(git rev-parse HEAD) \
  --build-arg KOHAKU_HUB_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker-compose up -d
```

Without them, `build.git_sha` is read from the checkout when the service runs from one, and is `null` otherwise.

## Security Configuration

It is **critical** to change the default secrets before deploying to production.

### Generate Secret Keys

You can generate secure random strings for your secrets using the following commands:

```bash
# Generate a 64-character random string for session and admin tokens
python scripts/generate_secret.py 64

# Or use openssl
openssl rand -base64 48
```

Update the following variables in your `docker-compose.yml` with the generated secrets:

- `KOHAKU_HUB_SESSION_SECRET`
- `KOHAKU_HUB_ADMIN_SECRET_TOKEN`
- `LAKEFS_AUTH_ENCRYPT_SECRET_KEY`

## Services

The Docker Compose setup includes the following services:

- **hub-ui**: Nginx server for the frontend application (port `28080`).
- **hub-api**: The main FastAPI backend (port `48888`).
- **khub-worker**: Runs durable background tasks from the same image as
  `hub-api` and shares its environment. `hub-api` runs migrations and writes
  the LakeFS credentials the worker reads. See
  [Background Tasks](../development/background-tasks.md).
- **postgres**: PostgreSQL database for metadata (port `5432`).
- **lakefs**: LakeFS for data versioning (port `28000`). Pinned to
  `treeverse/lakefs:1.86.0`; supported releases are 1.48.1 – 1.86.0, except
  1.70.0. See [LakeFS Compatibility](lakefs.md).
- **minio**: MinIO for S3-compatible object storage (ports `29000` and `29001`).

### Running several workers

`khub-worker` can run several replicas against the same queue. PostgreSQL
hands each task to exactly one of them (`FOR UPDATE SKIP LOCKED`), so you do
not need to copy the service. Pick the number at startup:

```bash
KOHAKU_HUB_WORKER_REPLICAS=3 docker compose up -d
# or
docker compose up -d --scale khub-worker=3
```

`KOHAKU_HUB_WORKER_REPLICAS` can also go in the `.env` file next to
`docker-compose.yml`. It defaults to 1. Run `up -d` again with another number
to add or remove replicas. A replica that is stopped hands its running tasks
back to the queue, and another replica continues them.

Things to keep in mind:

- **Container names.** Replicas are named `<project>-khub-worker-1`, `-2`, and
  so on, so the service sets no `container_name`. Read their logs together
  with `docker compose logs -f khub-worker`.
- **Seeing them.** Every replica registers itself. **Background Tasks →
  Workers** in the admin panel lists them by hostname (the container id) with
  their status and load, and the page header shows how many are online.
- **Total parallelism.** Tasks running at once add up to replicas ×
  `KOHAKU_HUB_WORKER_CONCURRENCY` (4 by default).
- **Database connections.** Each replica holds its own PostgreSQL connection,
  so check `max_connections` before running many.
- **SQLite.** On a SQLite deployment, run one replica: SQLite writes are
  serialized.
- **Several hosts.** Docker Compose sets a fixed number of replicas on one
  host; it does not scale automatically. To spread workers across machines,
  run `khub-worker` on each host against the same PostgreSQL, LakeFS and
  object storage.

## Managing the Application

### View Logs

To view the logs for all services, use:

```bash
docker-compose logs -f
```

To view the logs for a specific service, use:

```bash
docker-compose logs -f hub-api
```

### Stop the Services

To stop all running services, use:

```bash
docker-compose down
```

## Accessing the Application

- **Web UI**: `http://localhost:28080`
- **Admin Portal**: `http://localhost:28080/admin`
- **API Docs**: `http://localhost:48888/docs`
- **LakeFS UI**: `http://localhost:28000`
- **MinIO Console**: `http://localhost:29000`
