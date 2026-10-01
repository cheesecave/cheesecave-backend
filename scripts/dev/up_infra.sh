#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env.dev"

NETWORK_NAME="kohakuhub-dev"
POSTGRES_CONTAINER="kohakuhub-dev-postgres"
MINIO_CONTAINER="kohakuhub-dev-minio"
LAKEFS_CONTAINER="kohakuhub-dev-lakefs"
VALKEY_CONTAINER="kohakuhub-dev-valkey"

POSTGRES_DATA_DIR="${ROOT_DIR}/hub-meta/dev/postgres-data"
MINIO_DATA_DIR="${ROOT_DIR}/hub-meta/dev/minio-data"
MINIO_CONFIG_DIR="${ROOT_DIR}/hub-meta/dev/minio-config"
LAKEFS_DATA_DIR="${ROOT_DIR}/hub-meta/dev/lakefs-data"
LAKEFS_CACHE_DIR="${ROOT_DIR}/hub-meta/dev/lakefs-cache"
VALKEY_DATA_DIR="${ROOT_DIR}/hub-meta/dev/valkey-data"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

: "${DEV_POSTGRES_USER:=hub_dev}"
: "${DEV_POSTGRES_PASSWORD:=hub_dev_password}"
: "${DEV_POSTGRES_DB:=kohakuhub_dev}"
: "${DEV_MINIO_ROOT_USER:=minioadmin}"
: "${DEV_MINIO_ROOT_PASSWORD:=minioadmin}"
# MinIO must advertise CORS so the SPA can do cross-origin Range reads on
# presigned /resolve/ targets for the pure-client preview (issue #27).
: "${DEV_MINIO_CORS_ALLOW_ORIGIN:=*}"
: "${DEV_LAKEFS_ENCRYPT_SECRET_KEY:=dev-lakefs-encrypt-key-32chars}"
: "${KOHAKU_HUB_S3_BUCKET:=hub-storage}"
: "${KOHAKU_HUB_S3_REGION:=us-east-1}"
: "${KOHAKU_HUB_S3_ACCESS_KEY:=${DEV_MINIO_ROOT_USER}}"
: "${KOHAKU_HUB_S3_SECRET_KEY:=${DEV_MINIO_ROOT_PASSWORD}}"

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fxq "$1"
}

docker_is_rootless() {
  docker info --format '{{range .SecurityOptions}}{{.}} {{end}}' 2>/dev/null |
    grep -q 'name=rootless'
}

container_running() {
  docker ps --format '{{.Names}}' | grep -Fxq "$1"
}

ensure_network() {
  if ! docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
    docker network create "${NETWORK_NAME}" >/dev/null
    echo "Created network ${NETWORK_NAME}"
  fi
}

ensure_directories() {
  # Keep state on the host so recreating containers does not reset local dev data.
  mkdir -p \
    "${POSTGRES_DATA_DIR}" \
    "${MINIO_DATA_DIR}" \
    "${MINIO_CONFIG_DIR}" \
    "${LAKEFS_DATA_DIR}" \
    "${LAKEFS_CACHE_DIR}" \
    "${VALKEY_DATA_DIR}"
}

ensure_postgres() {
  if container_running "${POSTGRES_CONTAINER}"; then
    echo "Postgres already running"
    return
  fi

  if container_exists "${POSTGRES_CONTAINER}"; then
    docker start "${POSTGRES_CONTAINER}" >/dev/null
    echo "Started existing ${POSTGRES_CONTAINER}"
    return
  fi

  docker run -d \
    --name "${POSTGRES_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    -p 25432:5432 \
    -e POSTGRES_USER="${DEV_POSTGRES_USER}" \
    -e POSTGRES_PASSWORD="${DEV_POSTGRES_PASSWORD}" \
    -e POSTGRES_DB="${DEV_POSTGRES_DB}" \
    -v "${POSTGRES_DATA_DIR}:/var/lib/postgresql/data" \
    postgres:15 >/dev/null

  echo "Created ${POSTGRES_CONTAINER}"
}

ensure_minio() {
  if container_running "${MINIO_CONTAINER}"; then
    echo "MinIO already running"
    return
  fi

  if container_exists "${MINIO_CONTAINER}"; then
    docker start "${MINIO_CONTAINER}" >/dev/null
    echo "Started existing ${MINIO_CONTAINER}"
    return
  fi

  # MinIO: community-maintained build; quay.io/minio/minio no longer serves anonymous pulls.
  docker run -d \
    --name "${MINIO_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    -p 29001:9000 \
    -p 29000:29000 \
    -e MINIO_ROOT_USER="${DEV_MINIO_ROOT_USER}" \
    -e MINIO_ROOT_PASSWORD="${DEV_MINIO_ROOT_PASSWORD}" \
    -e MINIO_API_CORS_ALLOW_ORIGIN="${DEV_MINIO_CORS_ALLOW_ORIGIN}" \
    -v "${MINIO_DATA_DIR}:/data" \
    -v "${MINIO_CONFIG_DIR}:/root/.minio" \
    pgsty/minio:RELEASE.2026-08-04T00-00-00Z \
    server /data --console-address ":29000" >/dev/null

  echo "Created ${MINIO_CONTAINER}"
}

ensure_lakefs() {
  if container_running "${LAKEFS_CONTAINER}"; then
    echo "LakeFS already running"
    return
  fi

  if container_exists "${LAKEFS_CONTAINER}"; then
    # LakeFS config is stateless here; recreate the container and keep the bind-mounted data.
    docker rm -f "${LAKEFS_CONTAINER}" >/dev/null
    echo "Recreated ${LAKEFS_CONTAINER}"
  fi

  # Match the host user so LakeFS can write to the persisted metadata directory.
  # That uid has no /etc/passwd entry in the image, so Docker falls back to
  # HOME=/ and the config's "~/lakefs/data/cache" lands on the cache bind mount.
  #
  # Rootless Docker cannot use the host ids: they sit outside the container's id
  # mapping and runc aborts with "setgroups: invalid argument". There, container
  # root is already mapped to the invoking host user, so run as root and set
  # HOME explicitly to keep the cache path pointing at the same bind mount
  # (the image default user would be lakefs/uid 100, which can write neither).
  local -a user_args=()
  if docker_is_rootless; then
    echo "Rootless Docker detected; running ${LAKEFS_CONTAINER} as container root"
    user_args=(--user 0:0 -e HOME=/)
  else
    user_args=(--user "$(id -u):$(id -g)")
  fi

  # A Docker client with a "proxies" block in ~/.docker/config.json injects
  # HTTP_PROXY/NO_PROXY into every container. LakeFS reaches MinIO by container
  # name, and NO_PROXY is matched against the hostname rather than the resolved
  # address, so the container-network CIDRs listed there do not exempt it: the
  # S3 calls get routed to the host proxy, which cannot resolve an internal
  # container name, and repository creation fails with a 503. Keep this
  # container-to-container hop direct.
  local no_proxy_value="${MINIO_CONTAINER},localhost,127.0.0.1,::1"
  if [[ -n "${NO_PROXY:-${no_proxy:-}}" ]]; then
    no_proxy_value="${MINIO_CONTAINER},${NO_PROXY:-${no_proxy}}"
  fi

  docker run -d \
    --name "${LAKEFS_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    ${user_args[@]+"${user_args[@]}"} \
    -e "NO_PROXY=${no_proxy_value}" \
    -e "no_proxy=${no_proxy_value}" \
    -p 28000:28000 \
    -e LAKEFS_DATABASE_TYPE=local \
    -e LAKEFS_DATABASE_LOCAL_PATH=/var/lakefs/data/metadata.db \
    -e LAKEFS_BLOCKSTORE_TYPE=s3 \
    -e "LAKEFS_BLOCKSTORE_S3_ENDPOINT=http://${MINIO_CONTAINER}:9000" \
    -e "LAKEFS_BLOCKSTORE_S3_BUCKET=${KOHAKU_HUB_S3_BUCKET}" \
    -e LAKEFS_BLOCKSTORE_S3_FORCE_PATH_STYLE=true \
    -e "LAKEFS_BLOCKSTORE_S3_CREDENTIALS_ACCESS_KEY_ID=${KOHAKU_HUB_S3_ACCESS_KEY}" \
    -e "LAKEFS_BLOCKSTORE_S3_CREDENTIALS_SECRET_ACCESS_KEY=${KOHAKU_HUB_S3_SECRET_KEY}" \
    -e "LAKEFS_BLOCKSTORE_S3_REGION=${KOHAKU_HUB_S3_REGION}" \
    -e "LAKEFS_AUTH_ENCRYPT_SECRET_KEY=${DEV_LAKEFS_ENCRYPT_SECRET_KEY}" \
    -e LAKEFS_LOGGING_FORMAT=text \
    -e LAKEFS_LISTEN_ADDRESS=0.0.0.0:28000 \
    -v "${LAKEFS_DATA_DIR}:/var/lakefs/data" \
    -v "${LAKEFS_CACHE_DIR}:/lakefs/data/cache" \
    treeverse/lakefs:1.86.0 >/dev/null  # docs/deployment/lakefs.md

  echo "Created ${LAKEFS_CONTAINER}"
}

ensure_valkey() {
  if container_running "${VALKEY_CONTAINER}"; then
    echo "Valkey already running"
    return
  fi

  if container_exists "${VALKEY_CONTAINER}"; then
    docker start "${VALKEY_CONTAINER}" >/dev/null
    echo "Started existing ${VALKEY_CONTAINER}"
    return
  fi

  # Mirror production deploy: RDB persistence + LFU eviction + 256MB cap
  # (smaller than prod's 512MB; dev workload is one user). Bind-mounting
  # to the host means a docker rm + ensure_valkey cycle keeps the warm
  # working set, which speeds up local iteration considerably.
  docker run -d \
    --name "${VALKEY_CONTAINER}" \
    --network "${NETWORK_NAME}" \
    -p 26379:6379 \
    -v "${VALKEY_DATA_DIR}:/data" \
    valkey/valkey:8-alpine \
    valkey-server \
    --maxmemory 256mb \
    --maxmemory-policy allkeys-lfu \
    --save 300 100 \
    --appendonly no >/dev/null

  echo "Created ${VALKEY_CONTAINER}"
}

wait_for_postgres() {
  until docker exec "${POSTGRES_CONTAINER}" pg_isready -U "${DEV_POSTGRES_USER}" -d "${DEV_POSTGRES_DB}" >/dev/null 2>&1; do
    sleep 1
  done
}

wait_for_valkey() {
  until docker exec "${VALKEY_CONTAINER}" valkey-cli ping >/dev/null 2>&1; do
    sleep 1
  done
}

wait_for_http() {
  local url="$1"
  until curl -fsS "${url}" >/dev/null 2>&1; do
    sleep 1
  done
}

ensure_network
ensure_directories
ensure_postgres
ensure_minio
ensure_lakefs
ensure_valkey

wait_for_postgres
wait_for_http "http://127.0.0.1:29001/minio/health/live"
wait_for_http "http://127.0.0.1:28000/_health"
wait_for_valkey

cat <<EOF
Infra is ready.

Postgres: postgresql://${DEV_POSTGRES_USER}:${DEV_POSTGRES_PASSWORD}@127.0.0.1:25432/${DEV_POSTGRES_DB}
MinIO API: http://127.0.0.1:29001
MinIO Console: http://127.0.0.1:29000
LakeFS: http://127.0.0.1:28000
Valkey: redis://127.0.0.1:26379/0

Next:
  1. cp .env.dev.example .env.dev
  2. ./scripts/dev/run_backend.sh
EOF
