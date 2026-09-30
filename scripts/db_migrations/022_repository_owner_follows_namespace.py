#!/usr/bin/env python3
"""
Migration 022: A repository belongs to the account its namespace names.

Changes (data only, no schema change):
- repository.owner_id: the user or organization whose name is the
  repository's namespace, where it was another account. Earlier versions set
  the owner to the creator, also for an organization's repositories, and kept
  it when a repository moved to another namespace (#107)
- file.owner_id and commit.owner_id of those repositories: the same account
  (both denormalize the repository's owner)

Why: deleting an account deletes every repository it owns, with its LakeFS
repository and storage. A member who created a repository in an organization,
or a user who moved one into an organization, took it along when their account
was deleted.

Storage usage is counted per namespace, not per owner, so it does not change
here. Usage an admin's move left behind (it used to skip moving it) is
recounted by the namespace's next commit, or by the admin quota recalculation.

A repository whose namespace no account holds is reported and left alone; if
an account with that name is created later, a later run of this migration
(while it is the newest) hands it over, as its permissions already follow the
namespace.

Plain SQL, not the models: this must keep meaning what it means today.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from kohakuhub.config import cfg
from kohakuhub.db import db
from _migration_utils import check_table_exists, should_skip_due_to_future_migrations

MIGRATION_NUMBER = 22

MISOWNED = (
    'SELECT r.id, r.repo_type, r.full_id, owner.username, account.id, account.username '
    'FROM "repository" r '
    'JOIN "user" account ON account.username = r.namespace '
    # LEFT: an owner row missing (an old SQLite database without foreign keys)
    'LEFT JOIN "user" owner ON owner.id = r.owner_id '
    "WHERE r.owner_id <> account.id"
)
ORPHANS = (
    'SELECT r.repo_type, r.full_id FROM "repository" r '
    'WHERE r.namespace NOT IN (SELECT username FROM "user")'
)


def is_applied(db, cfg):
    """Applied once the schema before it is complete (so earlier migrations
    never skip themselves on its account) and every repository whose namespace
    an account holds is owned by it."""
    if not check_table_exists(db, "lfs_gc_state"):
        return False
    return db.execute_sql(MISOWNED + " LIMIT 1").fetchone() is None


def _repair():
    """Hand every misowned repository, its files and its commits to its namespace."""
    p = db.param
    rows = db.execute_sql(MISOWNED).fetchall()
    for repo_id, repo_type, full_id, owner, account_id, account in rows:
        db.execute_sql(f'UPDATE "repository" SET owner_id = {p} WHERE id = {p}', (account_id, repo_id))
        db.execute_sql(f'UPDATE "file" SET owner_id = {p} WHERE repository_id = {p}', (account_id, repo_id))
        db.execute_sql(
            f'UPDATE "commit" SET owner_id = {p} WHERE repository_id = {p}', (account_id, repo_id)
        )
        print(f"  ✓ {repo_type}:{full_id}: owner {owner or 'missing'} -> {account}")
    return len(rows)


def run():
    """Run migration 022.

    Returns:
        True if successful or already applied, False otherwise
    """
    db.connect(reuse_if_open=True)

    try:
        if should_skip_due_to_future_migrations(MIGRATION_NUMBER, db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Skipped (superseded by future migration)")
            return True

        for repo_type, full_id in db.execute_sql(ORPHANS).fetchall():
            print(
                f"Migration {MIGRATION_NUMBER}: {repo_type}:{full_id} has no account for its "
                "namespace; left as it is"
            )

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (every repository owned by its namespace)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repositories belong to their namespace")
        print("=" * 70)
        with db.atomic():
            repaired = _repair()
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully ({repaired} repositories)")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
