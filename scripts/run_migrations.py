#!/usr/bin/env python3
"""
Main migration runner for KohakuHub database migrations.

This script automatically discovers and runs all migration scripts in the
db_migrations/ directory in numerical order (001, 002, 003, etc.).

Each migration script should:
1. Have a run() function that returns True on success, False on failure
2. Auto-detect if it needs to run (check if columns exist)
3. Handle both SQLite and PostgreSQL

Usage:
    python scripts/run_migrations.py
"""

import sys
import os
from pathlib import Path
import importlib.util
from urllib.parse import urlsplit, urlunsplit

# Add src to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

# Import after path setup
from kohakuhub.db import db, init_db
from kohakuhub.config import cfg


def redact_url(url: str) -> str:
    """``url`` with its password replaced: the runner's output goes to operators' logs."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urlunsplit(parts._replace(netloc=netloc))


class _RedactingStream:
    """A text stream that masks the secrets in whatever is written to it. The runner
    prints the URL in its header and migration modules print their own lines; a
    password in an exception text would reach the log the same way."""

    def __init__(self, stream, secrets):
        self._stream = stream
        self._secrets = secrets

    def write(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return self._stream.write(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def discover_migrations():
    """Discover all migration scripts in db_migrations/ directory.

    Returns:
        List of (name, path) tuples sorted by name
    """
    migrations_dir = SCRIPT_DIR / "db_migrations"
    if not migrations_dir.exists():
        return []

    migrations = []
    for file_path in migrations_dir.glob("*.py"):
        if file_path.name.startswith("_"):
            continue  # Skip __init__.py and private files
        migrations.append((file_path.stem, file_path))

    # Sort by name (which should be numerical like 001_, 002_, etc.)
    migrations.sort(key=lambda x: x[0])
    return migrations


def load_migration_module(name, path):
    """Dynamically load a migration module.

    Args:
        name: Module name
        path: Path to migration file

    Returns:
        Loaded module or None if failed
    """
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        print(f"  [ERROR] Failed to load {name}: {e}")
        return None


def is_database_initialized():
    """Check if database is initialized (has User table).

    Returns:
        True if User table exists (database is initialized)
        False if User table doesn't exist (fresh database)
    """
    try:
        db.connect(reuse_if_open=True)
        cursor = db.cursor()

        if cfg.app.db_backend == "postgres":
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_name='user'
                """
            )
            return cursor.fetchone() is not None
        else:
            # SQLite
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='user'"
            )
            return cursor.fetchone() is not None
    except Exception as e:
        print(f"  [WARNING] Failed to check database state: {e}")
        # If we can't check, assume uninitialized (safer to skip migrations)
        return False


def run_migrations():
    """Run all pending migrations."""
    print("=" * 70)
    print("KohakuHub Database Migrations")
    print("=" * 70)
    print(f"Database backend: {cfg.app.db_backend}")
    print(f"Database URL: {redact_url(cfg.app.database_url)}")
    print()

    # Check if database is completely uninitialized
    if not is_database_initialized():
        print("Database is uninitialized (User table doesn't exist)")
        print("Skipping all migrations - will create fresh schema via init_db()")
        print("\nInitializing database (creating all tables)...")
        init_db()
        print("✓ Database initialized with current schema\n")
        print("=" * 70)
        print("[OK] Fresh database initialized successfully!")
        print("=" * 70)
        return True

    # Database is initialized, check for migrations
    print("Database is initialized, checking for pending migrations...\n")

    # Discover migrations
    migrations = discover_migrations()
    if not migrations:
        print("No migrations found in db_migrations/")
        print("\nEnsuring database schema is up-to-date...")
        init_db()
        print("✓ Database schema verified\n")
        return True

    print(f"Found {len(migrations)} migration(s):\n")

    # Run each migration
    all_success = True
    for name, path in migrations:
        print(f"Running {name}...")

        # Load migration module
        module = load_migration_module(name, path)
        if not module:
            all_success = False
            continue

        # Check if module has run() function
        if not hasattr(module, "run"):
            print(f"  [ERROR] Migration {name} missing run() function")
            all_success = False
            continue

        # Run migration
        try:
            success = module.run()
            if not success:
                all_success = False
        except Exception as e:
            print(f"  [ERROR] Migration {name} crashed: {e}")
            import traceback

            traceback.print_exc()
            all_success = False

        print()

    # Initialize database AFTER migrations (create tables/indexes if needed)
    if all_success:
        print("\nFinalizing database schema (ensuring all tables/indexes exist)...")
        try:
            init_db()
            print("[OK] Database schema finalized\n")
        except Exception as e:
            print(f"[ERROR] Failed to finalize database schema: {e}")
            import traceback

            traceback.print_exc()
            all_success = False

    # Summary
    print("=" * 70)
    if all_success:
        print("[OK] All migrations completed successfully!")
    else:
        print("[ERROR] Some migrations failed - please check errors above")
    print("=" * 70)

    return all_success


def main():
    secrets = [p for p in [urlsplit(cfg.app.database_url).password] if p]
    sys.stdout = _RedactingStream(sys.stdout, secrets)
    sys.stderr = _RedactingStream(sys.stderr, secrets)
    try:
        success = run_migrations()
        return 0 if success else 1
    except Exception as e:
        print(f"\n[ERROR] Migration runner crashed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
