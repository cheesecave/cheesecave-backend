#!/usr/bin/env python3
"""
Shared utilities for database migrations.

This module provides common functionality for checking migration status
without importing any Peewee models (to avoid breaking old migrations).
"""

import os
from pathlib import Path


def should_skip_due_to_future_migrations(
    current_migration_number: int, db, cfg
) -> bool:
    """Check if any future migration has been applied, indicating this migration should skip.

    Args:
        current_migration_number: The number of the current migration (e.g., 3 for migration 003)
        db: Database connection object
        cfg: Config object with db_backend property

    Returns:
        True if any future migration is applied (skip current migration)
        False if no future migrations found (run current migration)

    How it works:
    1. Discovers all migration files with higher numbers
    2. Checks from newest to oldest if migration is applied
    3. If ANY future migration is applied, current migration should skip
    4. Errors/exceptions are treated as "not applied"
    """
    migrations_dir = Path(__file__).parent

    # Find all migration files with numbers greater than current
    future_migrations = []
    for file_path in migrations_dir.glob("*.py"):
        if file_path.name.startswith("_"):
            continue  # Skip utility files

        # Extract migration number (e.g., "003" from "003_commit_tracking.py")
        try:
            number_str = file_path.stem.split("_")[0]
            number = int(number_str)
            if number > current_migration_number:
                future_migrations.append((number, file_path))
        except (ValueError, IndexError):
            continue  # Skip files without valid number prefix

    if not future_migrations:
        # No future migrations found, don't skip
        return False

    # Sort from newest to oldest
    future_migrations.sort(reverse=True)

    # Check each future migration from newest to oldest
    for number, file_path in future_migrations:
        try:
            # Dynamically import the migration module
            import importlib.util
            import sys

            # Create a unique module name to avoid conflicts
            module_name = f"_temp_migration_{number:03d}"

            # Remove module if already loaded (cleanup from previous attempts)
            if module_name in sys.modules:
                del sys.modules[module_name]

            spec = importlib.util.spec_from_file_location(module_name, file_path)
            module = importlib.util.module_from_spec(spec)

            # Load module in isolated environment
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Check if migration has is_applied() function
            if hasattr(module, "is_applied"):
                try:
                    is_applied = module.is_applied(db, cfg)
                    if is_applied:
                        # Future migration is applied, current migration should skip
                        # Clean up module
                        if module_name in sys.modules:
                            del sys.modules[module_name]
                        return True
                except Exception as e:
                    # Error checking = treat as not applied, continue checking
                    # This is expected when checking migrations on old schema
                    pass

            # Clean up module
            if module_name in sys.modules:
                del sys.modules[module_name]

        except Exception as e:
            # Error loading module = treat as not applied, continue checking
            pass

    # No future migrations are applied, don't skip
    return False


def check_table_exists(db, table_name: str) -> bool:
    """Check if a table exists in the database.

    Args:
        db: Database connection object
        table_name: Name of the table to check

    Returns:
        True if table exists, False otherwise
    """
    try:
        return db.table_exists(table_name)
    except Exception:
        return False


def check_column_exists(db, cfg, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table.

    Args:
        db: Database connection object
        cfg: Config object with db_backend property
        table_name: Name of the table
        column_name: Name of the column to check

    Returns:
        True if column exists, False otherwise
    """
    try:
        cursor = db.cursor()
        if cfg.app.db_backend == "postgres":
            # Probe the table resolved by search_path, not a same-named table in another schema.
            cursor.execute(
                """
                SELECT attname
                FROM pg_catalog.pg_attribute
                WHERE attrelid=to_regclass(%s) AND attname=%s
                    AND attnum > 0 AND NOT attisdropped
            """,
                (table_name, column_name),
            )
            return cursor.fetchone() is not None
        else:
            # SQLite
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [row[1] for row in cursor.fetchall()]
            return column_name in columns
    except Exception:
        return False
