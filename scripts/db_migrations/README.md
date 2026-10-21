# Database Migrations

This directory contains database migration scripts for KohakuHub.

## How Migrations Work

1. **Auto-detection**: Each migration checks if it needs to run by verifying if columns/tables exist
2. **Sequential execution**: Migrations run in numerical order (001, 002, 003, etc.)
3. **Idempotent**: Safe to run multiple times - already-applied migrations are skipped
4. **Auto-run**: Migrations automatically run on container startup via `docker/startup.py`
5. **Self-healing**: Each migration automatically checks if ANY future migration has been applied
   - If migration 005 finds that migration 008 is applied, it skips (changes already included)
   - Works automatically for any future migrations (009, 010, etc.)
   - No hardcoding - migrations discover and check future migrations dynamically

## Migration Order

| # | Name | Description | Notes |
|---|------|-------------|-------|
| 001 | repository_schema | Remove unique constraint from Repository.full_id | Skipped if post-008 |
| 002 | user_org_quotas | Add private/public quota fields to User/Organization | Skipped if post-008 |
| 003 | commit_tracking | Add Commit table for tracking user commits | Skipped if post-008 |
| 004 | repo_quotas | Add quota/used_bytes fields to Repository | Skipped if post-008 |
| 005 | profiles_and_invitations | Add profile fields and invitation system | Skipped if post-008 |
| 006 | invitation_multi_use | Add multi-use support to invitations | Skipped if post-008 |
| 007 | avatar_support | Add avatar fields to User/Organization | Skipped if post-008 |
| 008 | foreignkey_refactoring | **BREAKING** Merge User/Organization tables + Add ForeignKeys | Major schema change |

## Migration 008 Schema Refactoring

**Migration 008 is a major schema refactoring that:**
- Merges the Organization table into User table (adds `is_org` flag)
- Converts all integer ID references to proper ForeignKey constraints
- Adds denormalized owner fields for performance

**If you have an existing database:**
- Migrations 001-007 will automatically skip (changes already included in 008)
- Migration 008 requires confirmation (or set `CHEESE_CAVE_AUTO_MIGRATE=true` in Docker)
- **BACKUP YOUR DATABASE BEFORE RUNNING 008**

**For fresh/new databases:**
- Recreate the database from scratch instead of running migrations:
  ```bash
  # Stop services
  docker-compose down

  # Remove old database data
  rm -rf hub-meta/postgres-data/*

  # Restart (will auto-create schema)
  docker-compose up -d
  ```
- Fresh databases get the latest schema automatically via `init_db()`
- All migrations will skip (nothing to migrate)

## Creating New Migrations

1. Create a new file: `scripts/db_migrations/00X_name.py`
2. Implement these functions:
   - `MIGRATION_NUMBER` - Constant with migration number (e.g., 9)
   - `is_applied(db, cfg)` - Check if THIS migration has been applied (for future migrations to detect)
   - `check_migration_needed()` - Returns True if migration should run
   - `migrate_sqlite()` - SQLite migration logic
   - `migrate_postgres()` - PostgreSQL migration logic
   - `run()` - Main entry point that uses `should_skip_due_to_future_migrations()`

3. Template:
```python
#!/usr/bin/env python3
"""Migration 00X: Description"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.db import db
from kohakuhub.config import cfg
from _migration_utils import should_skip_due_to_future_migrations, check_column_exists, check_table_exists

# IMPORTANT: Do NOT import Peewee models (User, Repository, etc.)
# Models may be renamed/deleted in future versions, breaking old migrations.
# Use raw SQL queries instead.

MIGRATION_NUMBER = X  # Replace X with actual number


def is_applied(db, cfg):
    """Check if THIS migration has been applied.

    This function is called by older migrations to detect if this migration
    has already applied their changes. Choose a unique signature column/table.

    Returns True if this migration is applied, False otherwise.
    Errors should return True (treat as applied, skip older migrations).
    """
    # Example: Check if a signature column/table exists
    return check_column_exists(db, cfg, "mytable", "mycolumn")


def check_migration_needed():
    """Check if this migration needs to run."""
    cursor = db.cursor()
    if cfg.app.db_backend == "postgres":
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='mytable' AND column_name='mycolumn'
        """)
        return cursor.fetchone() is None
    else:
        cursor.execute("PRAGMA table_info(mytable)")
        columns = [row[1] for row in cursor.fetchall()]
        return 'mycolumn' not in columns


def migrate_sqlite():
    cursor = db.cursor()
    cursor.execute("ALTER TABLE mytable ADD COLUMN mycolumn INTEGER")
    db.commit()


def migrate_postgres():
    cursor = db.cursor()
    cursor.execute("ALTER TABLE mytable ADD COLUMN mycolumn BIGINT")
    db.commit()


def run():
    db.connect(reuse_if_open=True)
    try:
        # Check if any future migration has been applied (auto-skip if superseded)
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print("Migration 00X: Skipped (superseded by future migration)")
            return True

        if not check_migration_needed():
            print("Migration 00X: Already applied")
            return True

        print("Migration 00X: Running...")
        if cfg.app.db_backend == "postgres":
            migrate_postgres()
        else:
            migrate_sqlite()
        print("Migration 00X: ✓ Completed")
        return True
    except Exception as e:
        print(f"Migration 00X: ✗ Failed - {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        db.close()

if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
```

## Running Migrations

**Automatic (in Docker):**
- Migrations run automatically on container startup

**Manual:**
```bash
# Run all migrations
python scripts/run_migrations.py

# Run specific migration
python scripts/db_migrations/001_repository_schema.py
```

## Best Practices

### For Fresh Databases
**Recommended:** Delete database data and let `init_db()` create the latest schema:
```bash
docker-compose down
rm -rf hub-meta/postgres-data/*
docker-compose up -d
```

This is faster and cleaner than running all migrations sequentially.

### For Existing Databases
1. **Backup first!** Always backup before running migrations
2. Run migrations via the automatic startup process
3. Set `CHEESE_CAVE_AUTO_MIGRATE=true` to skip confirmation prompts in Docker
4. Monitor logs for migration status

### For Development
```bash
# Run all pending migrations
python scripts/run_migrations.py

# Run specific migration
python scripts/db_migrations/001_repository_schema.py
```

## Migration System Design

### Self-Healing Future-Migration Detection

Each migration automatically checks if any **future** migration has been applied before running:

**How it works:**
1. Migration 003 is about to run
2. Checks migrations 008, 007, 006, 005, 004 (newest to oldest)
3. If migration 008's `is_applied()` returns True → skip migration 003
4. If all future migrations return False → run migration 003 normally

**Benefits:**
- No hardcoding of specific migration numbers
- Automatically works when you add migration 009, 010, etc.
- Errors/exceptions in `is_applied()` are treated as "not applied" (safe fallback)
- Makes migrations resilient to major schema refactorings

**Each migration must implement:**
```python
def is_applied(db, cfg):
    """Check if THIS migration has been applied.

    Choose a unique signature (table or column) that this migration creates.
    Errors should return True (treat as applied to be safe).
    """
    return check_column_exists(db, cfg, "mytable", "my_signature_column")
```

### Important Guidelines

#### DO NOT Import Peewee Models
**Never import models like `User`, `Repository`, `Organization`, etc. in migrations!**

```python
# ❌ BAD - Will break if model is renamed/deleted
from kohakuhub.db import db, User, Organization
db.create_tables([User], safe=True)

# ✅ GOOD - Use raw SQL instead
from kohakuhub.db import db
cursor = db.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS user (...)")
```

**Why?**
- Migrations are permanent historical records
- Models may be renamed, deleted, or refactored in future versions
- Importing models creates tight coupling that breaks old migrations
- Example: Migration 002 imported `Organization`, which no longer exists after migration 008

**Use raw SQL for all schema changes:**
- Table creation: `CREATE TABLE IF NOT EXISTS`
- Column addition: `ALTER TABLE ... ADD COLUMN`
- Index creation: `CREATE INDEX IF NOT EXISTS`

## Notes

- Migrations are idempotent - safe to re-run
- Failed migrations will prevent server startup
- Each migration auto-skips if ANY future migration has been applied
- Use raw SQL queries instead of importing Peewee models
- Errors in `is_applied()` are treated as "applied" (safe fallback)
- Old migration scripts in `scripts/migrate_*.py` are kept for reference

## Utilities (`_migration_utils.py`)

Common helper functions available to all migrations:

```python
from _migration_utils import (
    should_skip_due_to_future_migrations,  # Check if future migrations applied
    check_table_exists,                     # Check if table exists
    check_column_exists,                    # Check if column exists
)

# Usage in migration
if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
    print("Migration skipped - superseded by future migration")
    return True
```

## Troubleshooting

### Error: "column repository_id does not exist"

This error indicates the database is in an inconsistent state (mix of old and new schema).

**Cause:** The database has some tables created with the new schema (post-migration 008), but migrations never ran to update the data properly.

**Solution 1 (Recommended):** Drop database and start fresh:
```bash
docker-compose down
rm -rf hub-meta/postgres-data/*
docker-compose up -d
```

**Solution 2:** Manually fix the inconsistency:
1. Connect to database: `docker exec -it postgres psql -U hub -d hubdb`
2. Check schema: `\d file` and `\d repository`
3. If File table has `repository_id` but no data, drop and recreate:
   ```sql
   DROP TABLE IF EXISTS file CASCADE;
   DROP TABLE IF EXISTS repository CASCADE;
   -- Restart container to recreate tables
   ```

**Prevention:** Always run migrations before application starts (handled automatically in Docker)

## File branches (issue #11): 032 and 033

Both run with the other migrations at container start, in this order.

- **032** adds `file.branch` (default `main`) and the unique key
  `(repository_id, branch, path_in_repo)`. It keeps the old key `(repository_id, path_in_repo)`
  and creates the `schema_rollout` table. Nothing reads that table any more; it is left in place.
- **033** (`033_file_branch_drop_old_key.py`) makes the old key stop being unique, so a side
  branch can hold a row for a path `main` also has. Rows are not touched. The old index name stays
  taken by an empty non-unique index (`WHERE FALSE`): a pre-A image's `init_db()` would otherwise
  re-create the unique key with `CREATE UNIQUE INDEX IF NOT EXISTS`.
  It is applied when no unique index exists on `(repository_id, path_in_repo)`; a database built
  by `init_db()` never had one, so 033 is a no-op there.

033 cannot hang or queue: the DDL needs a brief exclusive lock on `file`, taken with a 2 s
`lock_timeout`. If a long transaction holds the table, it retries up to 6 times with backoff
(about 30 s) and then fails with `the lock on table file was not free in 6 attempts ...`;
the container stops and nothing has changed. End the transaction, restart the container, and
it completes. Two containers starting together are safe: the check is repeated under the lock,
so one drops the key and the other does nothing. It also refuses (and drops nothing) unless 032's
per-branch key exists and is valid, so the table never has no unique key; the runner goes on after a
failed migration, which is why this is checked. Until 033 has run, a side-branch commit that
touches a path `main` has answers 409 `BranchPathConflict` ("run the pending database
migrations").

Rollback facts and the command are in
[docs/deployment/production.md](../../docs/deployment/production.md#rollback-of-the-file-key-migration).

## Repository card (#62): 035

- **035** (`035_repository_card.py`) creates `repository_card`: one row per repository with the README front matter of the
  head commit it was read at (`card`, cleaned, as JSON; `"{}"` for a repository without a card), that head (`main_sha`),
  and what the card declares as fields of their own (`derived`: tags, pipeline_tag, library_name, sdk).
- **A table of its own on purpose.** Migration 029 compares the columns of `repository_metadata` exactly, so a column added
  there made every later run of the migrations (and an older release's run after a rollback) report a failure; the drill
  for this migration found it. The older code does not know the new table and ignores it, so rolling back is leaving it.
- Nothing is backfilled. A repository without a row is read the first time its info is asked for (`info_card`: a README
  prefix read with a 4 s budget) and by the next scheduled index, like any snapshot that is due. Until then the info answers
  `tags: []` and no `cardData`, as before the migration.
- `CREATE TABLE IF NOT EXISTS` on an empty table; two containers starting together take turns on an advisory lock.

## Repository derived info (#63): 036

- **036** (`036_repository_derived_info.py`) adds two columns to `repository_card` (the table of 035, never
  `repository_metadata`): `info` (what the files add and only the repository's own info shows: `config`, JSON,
  `"{}"` by default) and `version` (the version of the rules that derived the row, `0` by default).
- Nothing is backfilled. Every row of 035 has version `0`, which the code reads as "derived without the files":
  the next info request of that repository derives it again, and the scheduled refresh counts it as due, so a
  deploy re-derives the catalog a page of forty at a time. A change of the rules bumps `repo_derive.VERSION`, not a
  migration.
- Older code does not know the columns and ignores them: rolling back is leaving them. Each column is added only
  when it is missing, so a run that stopped between the two finishes; two containers starting together take turns
  on an advisory lock.
- **No migration for the weights summary (#63, part 3).** It lives in `repository_card.info` (`"safetensors"`), the column 036
  made. The release that adds it raises `repo_derive.VERSION`, so the rows of 036 are derived again on the next read or refresh
  (forty repositories a page, as for 036 itself) and each model with weights then reads their headers once, in the background.
  Rolling back is leaving the key: older code ignores `info`.

## User preferences: 037

- **037** (`037_user_preferences.py`) creates `user_preference`: one row per account that chose something (`data`, JSON, only the
  keys that were set), gone with the account. No row means the reader follows the site's defaults.
- **A table of its own on purpose**, as for 035: an older release does not know it and ignores it, so rolling back is leaving it.
- Nothing is backfilled. `CREATE TABLE IF NOT EXISTS`; two containers starting together take turns on an advisory lock.
- The site's default style needs no migration: it is a field of the `theme` section of `site_appearance` that is filled in from the
  code when a stored theme lacks it.

## Admin token throttle (#115): 038

- **038** (`038_admin_auth_client.py`) creates `admin_auth_client`: one row per client address (an IPv4 address or an IPv6 /64) that sent a
  wrong admin token lately (`failures`, `last_failure`, `wait_until`). Every worker process and replica shares the rows, so the wait does not
  depend on which worker answers. No secret is stored (no token, no hash). The code removes the rows of clients that have been quiet for twice
  the reset time.
- **A table of its own on purpose**, as for 035 and 037: an older release does not know it and ignores it (its throttle counts in process
  memory), so rolling back is leaving it. Nothing is backfilled. `CREATE TABLE IF NOT EXISTS`; two containers starting together take turns on an
  advisory lock. If the table cannot be read or written, the throttle falls back to the memory of the process and says so in the log.

## Password reset links: 039

- **039** (`039_password_reset_tokens.py`) creates `password_reset_token`: one row per one-time reset link an administrator made
  (`token_hash`, the hash of the token and never the token; `expires_at`; `used_at`, set when the link is used and kept as
  history; `created_by`), gone with the account.
- **A table of its own on purpose**, as for 035, 037 and 038: an older release does not know it and ignores it, so rolling back is
  leaving it.
- Nothing is backfilled. `CREATE TABLE IF NOT EXISTS` and two indexes; two containers starting together take turns on an
  advisory lock.

## Repository creators: 040

- **040** (`040_repository_creator.py`) creates `repository_creator`: one row per repository (`repository_id`, the key, gone
  with the repository; `user_id`, the person who created it, released when their account goes; `source`, `recorded` when the
  request that created the repository wrote it and `inferred` when this migration did).
- **Filled once for the repositories that exist:** the creator of an old repository is the author of its first commit on `main`
  when that commit came within a day of the repository; otherwise the repository has no row and its creator is unknown. New
  repositories are written by the request that creates them and are never touched by the migration.
- **A table of its own on purpose**, as for 035, 037, 038 and 039: an older release does not know it and ignores it, so rolling
  back is leaving it. The fill adds rows only for repositories that have none, so it can run again, or after `init_db()` built
  the empty table, without harm. `CREATE TABLE IF NOT EXISTS`; two containers starting together take turns on an advisory lock.
