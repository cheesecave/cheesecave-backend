#!/bin/sh
# Database initialization script for KohakuHub
# Creates both hub-api and LakeFS databases if they don't exist
# Runs before LakeFS starts

set -e

echo "=== KohakuHub Database Initialization ==="

# The URL is CHEESE_CAVE_DATABASE_URL, or the older KOHAKU_HUB_DATABASE_URL when that is not set
# (an empty value counts as set), the same rule as kohakuhub.envnames. This image has no Python.
DATABASE_URL="${CHEESE_CAVE_DATABASE_URL-${KOHAKU_HUB_DATABASE_URL-}}"

# Extract PostgreSQL connection details from DATABASE_URL or individual vars
if [ -n "$DATABASE_URL" ]; then
    # Parse connection string: postgresql://user:pass@host:port/dbname
    POSTGRES_USER=$(echo "$DATABASE_URL" | sed -n 's|.*://\([^:]*\):.*|\1|p')
    POSTGRES_PASSWORD=$(echo "$DATABASE_URL" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
    POSTGRES_HOST=$(echo "$DATABASE_URL" | sed -n 's|.*@\([^:]*\):.*|\1|p')
    POSTGRES_PORT=$(echo "$DATABASE_URL" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')
    POSTGRES_DB=$(echo "$DATABASE_URL" | sed -n 's|.*/\([^?]*\).*|\1|p')
else
    POSTGRES_USER="${POSTGRES_USER:-hub}"
    POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-hubpass}"
    POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
    POSTGRES_PORT="${POSTGRES_PORT:-5432}"
    POSTGRES_DB="${POSTGRES_DB:-kohakuhub}"
fi

LAKEFS_DB="${LAKEFS_DB:-lakefs}"

echo "PostgreSQL Server: $POSTGRES_HOST:$POSTGRES_PORT"
echo "Hub-API Database: $POSTGRES_DB"
echo "LakeFS Database: $LAKEFS_DB"
echo

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
max_attempts=30
attempt=0

while [ $attempt -lt $max_attempts ]; do
    if PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "SELECT 1" >/dev/null 2>&1; then
        echo "✓ PostgreSQL is ready"
        break
    fi
    attempt=$((attempt + 1))
    echo "  Attempt $attempt/$max_attempts..."
    sleep 2
done

if [ $attempt -eq $max_attempts ]; then
    echo "✗ PostgreSQL is not ready after $max_attempts attempts"
    echo "⚠ Continuing anyway, but LakeFS may fail to start..."
    exit 0
fi

echo

# Function to check if database exists
db_exists() {
    local dbname=$1
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$dbname'" 2>/dev/null
}

# Function to create database
create_db() {
    local dbname=$1
    echo "Creating database '$dbname'..."
    if PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $dbname;" >/dev/null 2>&1; then
        echo "✓ Created database '$dbname'"
        return 0
    else
        echo "⚠ Failed to create database '$dbname' (may already exist or insufficient permissions)"
        return 1
    fi
}

# Create hub-api database if it doesn't exist
echo "Checking hub-api database..."
if [ "$(db_exists "$POSTGRES_DB")" = "1" ]; then
    echo "✓ Database '$POSTGRES_DB' already exists"
else
    create_db "$POSTGRES_DB"
fi

echo

# Create LakeFS database if it doesn't exist (and it's different from hub-api db)
if [ "$LAKEFS_DB" != "$POSTGRES_DB" ]; then
    echo "Checking LakeFS database..."
    if [ "$(db_exists "$LAKEFS_DB")" = "1" ]; then
        echo "✓ Database '$LAKEFS_DB' already exists"
    else
        create_db "$LAKEFS_DB"
    fi
else
    echo "ℹ Using same database for both hub-api and LakeFS: $POSTGRES_DB"
fi

echo
echo "=== Database initialization complete ==="
echo
