#!/usr/bin/env python3
"""
Migration 008: Major database refactoring - Merge User/Organization + Add ForeignKeys.

BREAKING CHANGE: This is a major schema refactoring.
BACKUP YOUR DATABASE BEFORE RUNNING THIS MIGRATION!

Changes:
1. Merge Organization table into User table (add is_org flag)
2. Convert all integer ID fields to proper ForeignKey constraints
3. Add owner fields to File and Commit for denormalized access

New schema:
- User.is_org: distinguishes users (FALSE) from organizations (TRUE)
- EmailVerification.user: ForeignKey to User
- Session.user: ForeignKey to User
- Token.user: ForeignKey to User
- Repository.owner: ForeignKey to User (can be user or org)
- File.repository: ForeignKey to Repository
- File.owner: ForeignKey to User (denormalized from repository.owner)
- StagingUpload.repository: ForeignKey to Repository
- StagingUpload.uploader: ForeignKey to User
- UserOrganization.user: ForeignKey to User
- UserOrganization.organization: ForeignKey to User (is_org=TRUE)
- Commit.repository: ForeignKey to Repository
- Commit.author: ForeignKey to User (who made commit)
- Commit.owner: ForeignKey to User (repository owner, denormalized)
- LFSObjectHistory.repository: ForeignKey to Repository
- LFSObjectHistory.file: ForeignKey to File (nullable)
- SSHKey.user: ForeignKey to User
- Invitation.created_by: ForeignKey to User
- Invitation.used_by: ForeignKey to User (nullable)

This migration cannot be easily rolled back. Test thoroughly before deploying to production!
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.db import db
from kohakuhub.config import cfg
from kohakuhub.envnames import read_env
from _migration_utils import should_skip_due_to_future_migrations

MIGRATION_NUMBER = 8


def auto_confirm_enabled() -> bool:
    """CHEESE_CAVE_AUTO_MIGRATE (or the older KOHAKU_HUB_AUTO_MIGRATE): skip the prompt (Docker, CI)."""
    return (read_env("AUTO_MIGRATE") or "").lower() in ("true", "1", "yes")


def is_applied(db, cfg):
    """Check if THIS migration has been applied.

    Returns True if User.is_org column exists (signature of migration 008).

    NOTE: We implement this inline without importing check_column_exists
    to avoid any potential issues with circular imports or schema mismatches.
    """
    try:
        cursor = db.cursor()
        if cfg.app.db_backend == "postgres":
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='user' AND column_name='is_org'
            """,
            )
            return cursor.fetchone() is not None
        else:
            # SQLite
            cursor.execute("PRAGMA table_info(user)")
            columns = [row[1] for row in cursor.fetchall()]
            return "is_org" in columns
    except Exception:
        # Error = treat as applied (safe fallback to skip this migration)
        return True


def check_migration_needed():
    """Check if this migration needs to run.

    Returns True only if:
    - User table exists (schema version > 0)
    - AND User.is_org doesn't exist (schema version < 8)

    Returns False if:
    - User table doesn't exist (fresh install, version 0, will be created by init_db)
    - OR User.is_org exists (already at version 8+)
    """
    cursor = db.cursor()

    if cfg.app.db_backend == "postgres":
        # First check if user table exists
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name='user'
        """
        )
        if cursor.fetchone() is None:
            # Fresh database, tables will be created by init_db() with final schema
            return False

        # Table exists, check if is_org column exists
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='user' AND column_name='is_org'
        """
        )
        return cursor.fetchone() is None
    else:
        # SQLite: First check if table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user'"
        )
        if cursor.fetchone() is None:
            # Fresh database, tables will be created by init_db() with final schema
            return False

        # Table exists, check via PRAGMA if is_org column exists
        cursor.execute("PRAGMA table_info(user)")
        columns = [row[1] for row in cursor.fetchall()]
        return "is_org" not in columns


def migrate_sqlite():
    """Migrate SQLite database.

    NOTE: This function runs inside a transaction (db.atomic()).
    Do NOT call db.commit() or db.rollback() inside this function.

    Strategy:
    1. Add new columns to User table (is_org, description, make email/password nullable)
    2. Migrate Organization data into User table
    3. Create temporary mapping table for old org IDs
    4. Update all FK references
    5. Drop Organization table
    6. Rebuild tables with proper ForeignKey constraints
    """
    cursor = db.cursor()

    print("\n=== Phase 1: Add new columns to User table ===")

    # Add is_org column
    try:
        cursor.execute("ALTER TABLE user ADD COLUMN is_org BOOLEAN DEFAULT FALSE")
        print("  ✓ Added User.is_org")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print("  - User.is_org already exists")
        else:
            raise

    # Add description column (for orgs)
    try:
        cursor.execute("ALTER TABLE user ADD COLUMN description TEXT DEFAULT NULL")
        print("  ✓ Added User.description")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print("  - User.description already exists")
        else:
            raise

    # Add normalized_name column (for O(1) conflict checking)
    try:
        cursor.execute("ALTER TABLE user ADD COLUMN normalized_name TEXT")
        print("  ✓ Added User.normalized_name")
    except Exception as e:
        if "duplicate column" in str(e).lower():
            print("  - User.normalized_name already exists")
        else:
            raise

    # Note: SQLite doesn't support ALTER COLUMN to make existing columns nullable
    # This will require table recreation, which we'll handle in a full rebuild

    # Populate normalized_name for existing users
    print("\n  Populating User.normalized_name for existing users...")
    cursor.execute("SELECT id, username FROM user")
    users = cursor.fetchall()

    for user_id, username in users:
        # Normalize: lowercase, remove hyphens and underscores
        normalized = username.lower().replace("-", "").replace("_", "")
        cursor.execute(
            "UPDATE user SET normalized_name = ? WHERE id = ?", (normalized, user_id)
        )

    print(f"    ✓ Populated normalized_name for {len(users)} existing users")

    print("\n=== Phase 3: Migrate Organization data into User table ===")

    # Check if organization table exists
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='organization'"
    )
    if cursor.fetchone():
        # Get all organizations
        cursor.execute(
            "SELECT id, name, description, private_quota_bytes, public_quota_bytes, "
            "private_used_bytes, public_used_bytes, bio, website, social_media, "
            "avatar, avatar_updated_at, created_at FROM organization"
        )
        orgs = cursor.fetchall()

        print(f"  Found {len(orgs)} organization(s) to migrate")

        # Create mapping table for old org IDs -> new user IDs
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS _org_id_mapping (old_org_id INTEGER, new_user_id INTEGER)"
        )

        for org in orgs:
            (
                org_id,
                name,
                description,
                private_quota_bytes,
                public_quota_bytes,
                private_used_bytes,
                public_used_bytes,
                bio,
                website,
                social_media,
                avatar,
                avatar_updated_at,
                created_at,
            ) = org

            # Normalize name for conflict checking
            normalized = name.lower().replace("-", "").replace("_", "")

            # Insert organization as user with is_org=TRUE
            # email and password_hash will be NULL for organizations
            cursor.execute(
                """
                INSERT INTO user (username, normalized_name, is_org, email, password_hash, email_verified, is_active,
                                 private_quota_bytes, public_quota_bytes, private_used_bytes, public_used_bytes,
                                 full_name, bio, description, website, social_media,
                                 avatar, avatar_updated_at, created_at)
                VALUES (?, ?, TRUE, NULL, NULL, FALSE, TRUE, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    name,
                    normalized,
                    private_quota_bytes,
                    public_quota_bytes,
                    private_used_bytes,
                    public_used_bytes,
                    bio,
                    description,
                    website,
                    social_media,
                    avatar,
                    avatar_updated_at,
                    created_at,
                ),
            )

            new_user_id = cursor.lastrowid

            # Store mapping
            cursor.execute(
                "INSERT INTO _org_id_mapping (old_org_id, new_user_id) VALUES (?, ?)",
                (org_id, new_user_id),
            )

            print(f"  ✓ Migrated organization '{name}' (id {org_id} -> {new_user_id})")

            print(f"  ✓ All {len(orgs)} organizations migrated to User table")
    else:
        print("  - No organization table found, skipping")

    print("\n=== Phase 4: Update Foreign Key references ===")

    # 4a. Update UserOrganization.organization_id to reference new User IDs
    # NOTE: Peewee ForeignKeyField creates columns with _id suffix
    cursor.execute("SELECT id, organization_id FROM userorganization")
    memberships = cursor.fetchall()

    for membership_id, old_org_id in memberships:
        cursor.execute(
            "SELECT new_user_id FROM _org_id_mapping WHERE old_org_id = ?",
            (old_org_id,),
        )
        result = cursor.fetchone()
        if result:
            new_user_id = result[0]
            cursor.execute(
                "UPDATE userorganization SET organization_id = ? WHERE id = ?",
                (new_user_id, membership_id),
            )

    print(f"  ✓ Updated {len(memberships)} UserOrganization records")

    # 4b. Add owner column to File table (denormalized from repository.owner)
    print("  Adding File.owner_id column...")
    try:
        cursor.execute("ALTER TABLE file ADD COLUMN owner_id INTEGER")
        print("    ✓ Added File.owner_id column")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
        print("    - File.owner_id already exists")

    # Update File.owner_id from Repository.owner_id
    cursor.execute(
        """
        UPDATE file SET owner_id = (
            SELECT owner_id FROM repository
            WHERE repository.full_id = file.repo_full_id
            LIMIT 1
        )
    """
    )
    print(f"    ✓ Updated File.owner_id for all files")

    # 4c. Add owner column to Commit table (repository owner)
    print("  Adding Commit.owner_id column...")
    try:
        cursor.execute("ALTER TABLE commit ADD COLUMN owner_id INTEGER")
        print("    ✓ Added Commit.owner_id column")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
        print("    - Commit.owner_id already exists")

    # Update Commit.owner_id from Repository.owner_id
    cursor.execute(
        """
        UPDATE commit SET owner_id = (
            SELECT owner_id FROM repository
            WHERE repository.full_id = commit.repo_full_id
            LIMIT 1
        )
    """
    )
    print(f"    ✓ Updated Commit.owner_id for all commits")

    # 4d. Add uploader column to StagingUpload table
    print("  Adding StagingUpload.uploader_id column...")
    try:
        cursor.execute(
            "ALTER TABLE stagingupload ADD COLUMN uploader_id INTEGER DEFAULT NULL"
        )
        print("    ✓ Added StagingUpload.uploader_id column")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
        print("    - StagingUpload.uploader_id already exists")

    # 4e. Add file FK column to LFSObjectHistory table
    print("  Adding LFSObjectHistory.file_id column...")
    try:
        cursor.execute(
            "ALTER TABLE lfsobjecthistory ADD COLUMN file_id INTEGER DEFAULT NULL"
        )
        print("    ✓ Added LFSObjectHistory.file_id column")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
        print("    - LFSObjectHistory.file_id already exists")

    # Update LFSObjectHistory.file_id from File table
    cursor.execute(
        """
        UPDATE lfsobjecthistory SET file_id = (
            SELECT id FROM file
            WHERE file.repo_full_id = lfsobjecthistory.repo_full_id
            AND file.path_in_repo = lfsobjecthistory.path_in_repo
            LIMIT 1
        )
    """
    )
    print(f"    ✓ Updated LFSObjectHistory.file_id for all records")

    print("\n=== Phase 4: Cleanup ===")

    # Drop temporary mapping table
    try:
        cursor.execute("DROP TABLE _org_id_mapping")
        print("  ✓ Dropped temporary mapping table")
    except Exception as e:
        print(f"  - Failed to drop mapping table (non-fatal): {e}")

    # Drop Organization table
    try:
        cursor.execute("DROP TABLE organization")
        print("  ✓ Dropped Organization table")
    except Exception as e:
        print(f"  - Failed to drop organization table: {e}")
        # Non-fatal, continue

    print("\n⚠️  IMPORTANT: Foreign key constraints require table recreation in SQLite")
    print("⚠️  Peewee will handle this automatically on next application startup")
    print("⚠️  The application will recreate tables with proper ForeignKey constraints")

    return True


def migrate_postgres():
    """Migrate PostgreSQL database."""
    cursor = db.cursor()

    print("\n=== Phase 1: Backup Warning ===")
    print("⚠️  This migration modifies the database schema significantly.")
    print("⚠️  BACKUP YOUR DATABASE before proceeding!")
    print("")

    # Allow auto-confirmation via environment variable (for Docker/CI)
    auto_confirm = auto_confirm_enabled()
    if auto_confirm:
        print("  Auto-confirmation enabled (CHEESE_CAVE_AUTO_MIGRATE=true)")
        response = "yes"
    else:
        response = input("Type 'yes' to continue: ")

    if response.lower() != "yes":
        print("Migration cancelled.")
        return False

    print("\n=== Phase 2: Add new columns to User table ===")

    # Add is_org column
    try:
        cursor.execute('ALTER TABLE "user" ADD COLUMN is_org BOOLEAN DEFAULT FALSE')
        print("  ✓ Added User.is_org")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("  - User.is_org already exists")
        else:
            raise

    # Add description column
    try:
        cursor.execute('ALTER TABLE "user" ADD COLUMN description TEXT DEFAULT NULL')
        print("  ✓ Added User.description")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("  - User.description already exists")
        else:
            raise

    # Add normalized_name column (for O(1) conflict checking)
    try:
        cursor.execute('ALTER TABLE "user" ADD COLUMN normalized_name TEXT')
        print("  ✓ Added User.normalized_name")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("  - User.normalized_name already exists")
        else:
            raise

    # Make email and password_hash nullable
    try:
        cursor.execute('ALTER TABLE "user" ALTER COLUMN email DROP NOT NULL')
        cursor.execute('ALTER TABLE "user" ALTER COLUMN password_hash DROP NOT NULL')
        print("  ✓ Made email and password_hash nullable")
    except Exception as e:
        print(f"  - Failed to make columns nullable (may already be nullable): {e}")
        db.rollback()

    # Populate normalized_name for existing users
    print("  Populating User.normalized_name for existing users...")
    cursor.execute('SELECT id, username FROM "user"')
    users = cursor.fetchall()

    for user_id, username in users:
        # Normalize: lowercase, remove hyphens and underscores
        normalized = username.lower().replace("-", "").replace("_", "")
        cursor.execute(
            'UPDATE "user" SET normalized_name = %s WHERE id = %s',
            (normalized, user_id),
        )

    print(f"    ✓ Populated normalized_name for {len(users)} existing users")

    print("\n=== Phase 3: Migrate Organization data into User table ===")

    # Check if organization table exists
    cursor.execute(
        "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'organization')"
    )
    if cursor.fetchone()[0]:
        # Get all organizations
        cursor.execute(
            "SELECT id, name, description, private_quota_bytes, public_quota_bytes, "
            "private_used_bytes, public_used_bytes, bio, website, social_media, "
            "avatar, avatar_updated_at, created_at FROM organization"
        )
        orgs = cursor.fetchall()

        print(f"  Found {len(orgs)} organization(s) to migrate")

        # Create temporary mapping table
        cursor.execute(
            "CREATE TEMP TABLE _org_id_mapping (old_org_id INTEGER, new_user_id INTEGER)"
        )

        for org in orgs:
            (
                org_id,
                name,
                description,
                private_quota_bytes,
                public_quota_bytes,
                private_used_bytes,
                public_used_bytes,
                bio,
                website,
                social_media,
                avatar,
                avatar_updated_at,
                created_at,
            ) = org

            # Normalize name for conflict checking
            normalized = name.lower().replace("-", "").replace("_", "")

            # Insert organization as user with is_org=TRUE
            cursor.execute(
                """
                INSERT INTO "user" (username, normalized_name, is_org, email, password_hash, email_verified, is_active,
                                   private_quota_bytes, public_quota_bytes, private_used_bytes, public_used_bytes,
                                   full_name, bio, description, website, social_media,
                                   avatar, avatar_updated_at, created_at)
                VALUES (%s, %s, TRUE, NULL, NULL, FALSE, TRUE, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """,
                (
                    name,
                    normalized,
                    private_quota_bytes,
                    public_quota_bytes,
                    private_used_bytes,
                    public_used_bytes,
                    bio,
                    description,
                    website,
                    social_media,
                    avatar,
                    avatar_updated_at,
                    created_at,
                ),
            )

            new_user_id = cursor.fetchone()[0]

            # Store mapping
            cursor.execute(
                "INSERT INTO _org_id_mapping (old_org_id, new_user_id) VALUES (%s, %s)",
                (org_id, new_user_id),
            )

            print(f"  ✓ Migrated organization '{name}' (id {org_id} -> {new_user_id})")

            print(f"  ✓ All {len(orgs)} organizations migrated to User table")
    else:
        print("  - No organization table found, skipping")

    print("\n=== Phase 4: Update Foreign Key references ===")

    # 4a. Update UserOrganization.organization_id to reference new User IDs
    # NOTE: Peewee ForeignKeyField creates columns with _id suffix
    cursor.execute(
        "UPDATE userorganization SET organization_id = m.new_user_id "
        "FROM _org_id_mapping m WHERE userorganization.organization_id = m.old_org_id"
    )
    affected = cursor.rowcount
    print(f"  ✓ Updated {affected} UserOrganization records")

    # 4b. Add owner column to File table (denormalized from repository.owner)
    print("  Adding File.owner_id column...")
    try:
        cursor.execute("ALTER TABLE file ADD COLUMN owner_id INTEGER")
        print("    ✓ Added File.owner_id column")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("    - File.owner_id already exists")
        else:
            raise

    # Update File.owner_id from Repository.owner_id
    cursor.execute(
        """
        UPDATE file SET owner_id = repository.owner_id
        FROM repository
        WHERE repository.full_id = file.repo_full_id
    """
    )
    print(f"    ✓ Updated File.owner_id for all files")

    # 4c. Add owner column to Commit table (repository owner)
    print("  Adding Commit.owner_id column...")
    try:
        cursor.execute("ALTER TABLE commit ADD COLUMN owner_id INTEGER")
        print("    ✓ Added Commit.owner_id column")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("    - Commit.owner_id already exists")
        else:
            raise

    # Update Commit.owner_id from Repository.owner_id
    cursor.execute(
        """
        UPDATE commit SET owner_id = repository.owner_id
        FROM repository
        WHERE repository.full_id = commit.repo_full_id
    """
    )
    print(f"    ✓ Updated Commit.owner_id for all commits")

    # 4d. Add uploader column to StagingUpload table
    print("  Adding StagingUpload.uploader_id column...")
    try:
        cursor.execute(
            "ALTER TABLE stagingupload ADD COLUMN uploader_id INTEGER DEFAULT NULL"
        )
        print("    ✓ Added StagingUpload.uploader_id column")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("    - StagingUpload.uploader_id already exists")
        else:
            raise

    # 4e. Add file FK column to LFSObjectHistory table
    print("  Adding LFSObjectHistory.file_id column...")
    try:
        cursor.execute(
            "ALTER TABLE lfsobjecthistory ADD COLUMN file_id INTEGER DEFAULT NULL"
        )
        print("    ✓ Added LFSObjectHistory.file_id column")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("    - LFSObjectHistory.file_id already exists")
        else:
            raise

    # Update LFSObjectHistory.file_id from File table
    cursor.execute(
        """
        UPDATE lfsobjecthistory SET file_id = file.id
        FROM file
        WHERE file.repo_full_id = lfsobjecthistory.repo_full_id
        AND file.path_in_repo = lfsobjecthistory.path_in_repo
    """
    )
    print(f"    ✓ Updated LFSObjectHistory.file_id for all records")

    print("\n=== Phase 5: Drop old Organization table ===")

    try:
        cursor.execute("DROP TABLE IF EXISTS organization CASCADE")
        print("  ✓ Dropped Organization table")
    except Exception as e:
        print(f"  - Failed to drop organization table: {e}")

    print("\n⚠️  IMPORTANT: Table recreation with Foreign Keys")
    print("⚠️  Peewee ORM will handle ForeignKey constraint creation on next startup")
    print("⚠️  You may need to restart the application for changes to take effect")

    return True


def run():
    """Run this migration.

    IMPORTANT: Do NOT call db.close() in finally block!
    The db connection is managed by run_migrations.py and should stay open
    across all migrations to avoid stdout/stderr closure issues on Windows.

    NOTE: Migration 008 is special - user confirmation happens INSIDE migrate functions
    because it needs to check data before prompting. This is an exception to the normal
    pattern where confirmations happen before transactions.
    """
    db.connect(reuse_if_open=True)

    try:
        # Pre-flight checks (outside transaction for performance)
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print("Migration 008: Skipped (superseded by future migration)")
            return True

        migration_needed = check_migration_needed()
        if not migration_needed:
            # Check if table exists to provide better message
            cursor = db.cursor()
            if cfg.app.db_backend == "postgres":
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_name='user'"
                )
                table_exists = cursor.fetchone() is not None
            else:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='user'"
                )
                table_exists = cursor.fetchone() is not None

            if not table_exists:
                print(
                    "Migration 008: Skipped (user table doesn't exist yet, will be created by init_db)"
                )
            else:
                print("Migration 008: Already applied (User.is_org exists)")
            return True

        print("=" * 70)
        print("Migration 008: Major Database Refactoring")
        print("Merging User/Organization tables + Adding ForeignKey constraints")
        print("=" * 70)

        # NOTE: User confirmation happens INSIDE migrate functions for this migration
        # because it needs to analyze data first. Not wrapped in db.atomic() here
        # because the migrate functions handle their own transaction logic.
        if cfg.app.db_backend == "postgres":
            result = migrate_postgres()
        else:
            result = migrate_sqlite()

        if result:
            print("\n" + "=" * 70)
            print("Migration 008: ✓ Completed Successfully")
            print("=" * 70)
            print("\nNext steps:")
            print("1. Restart the application to apply ForeignKey constraints")
            print("2. Test all functionality thoroughly")
            print("3. Monitor logs for any foreign key constraint violations")

        return result

    except Exception as e:
        # If exception occurs, changes may have been partially applied
        print(f"\nMigration 008: ✗ Failed - {e}")
        print("  WARNING: This migration does not use db.atomic() due to user prompts")
        print("  Database may be in intermediate state - restore from backup!")
        import traceback

        traceback.print_exc()
        return False
    # NOTE: No finally block - db connection stays open


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
