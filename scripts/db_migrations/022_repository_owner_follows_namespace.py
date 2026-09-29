#!/usr/bin/env python3
"""
Migration 022: A repository belongs to the account its namespace names.

Changes (data only, no schema change):
- Repository.owner: the user or organization whose name is the repository's
  namespace, where it was another account. Earlier versions set the owner to
  the creator, also for an organization's repositories, and kept it when a
  repository moved to another namespace (#107)
- File.owner and Commit.owner of those repositories: the same account (both
  denormalize the repository's owner)
- The storage usage of every namespace involved, summed again from its
  repositories' usage: an admin's move used to leave it behind

Why: deleting an account deletes every repository it owns, with its LakeFS
repository and storage. A member who created a repository in an organization,
or a user who moved one into an organization, took it along when their account
was deleted.

A repository whose namespace no account holds is reported and left alone.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
# Add db_migrations to path (for _migration_utils)
sys.path.insert(0, os.path.dirname(__file__))

from peewee import fn

from kohakuhub.config import cfg
from kohakuhub.db import Commit, File, Repository, User, db
from _migration_utils import should_skip_due_to_future_migrations

MIGRATION_NUMBER = 22


def _misowned():
    """``(repository, the account its namespace names)`` where they differ."""
    Namespace = User.alias()
    return (
        Repository.select(Repository, Namespace)
        .join(Namespace, on=(Namespace.username == Repository.namespace), attr="account")
        .where(Repository.owner != Namespace.id)
    )


def _orphans():
    """Repositories whose namespace no account holds."""
    return Repository.select().where(
        Repository.namespace.not_in(User.select(User.username))
    )


def is_applied(db, cfg):
    """Applied once every repository whose namespace an account holds is owned by it."""
    return not _misowned().exists()


def _usage(namespace: str, private: bool) -> int:
    return (
        Repository.select(fn.COALESCE(fn.SUM(Repository.used_bytes), 0))
        .where((Repository.namespace == namespace) & (Repository.private == private))
        .scalar()
    )


def _repair():
    """Hand every misowned repository to its namespace; returns the namespaces involved."""
    involved = set()
    for repo in _misowned():
        account = repo.account
        involved.update({repo.namespace, repo.owner.username})
        Repository.update(owner=account).where(Repository.id == repo.id).execute()
        File.update(owner=account).where(File.repository == repo.id).execute()
        Commit.update(owner=account).where(Commit.repository == repo.id).execute()
        print(f"  ✓ {repo.repo_type}:{repo.full_id}: owner {repo.owner.username} -> {account.username}")
    for namespace in sorted(involved):
        User.update(
            private_used_bytes=_usage(namespace, True),
            public_used_bytes=_usage(namespace, False),
        ).where(User.username == namespace).execute()
    print(f"  ✓ Storage usage summed again for {len(involved)} namespace(s)")
    return involved


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

        for repo in _orphans():
            print(
                f"Migration {MIGRATION_NUMBER}: {repo.repo_type}:{repo.full_id} has no "
                "account for its namespace; left as it is"
            )

        if is_applied(db, cfg):
            print(f"Migration {MIGRATION_NUMBER}: Already applied (every repository owned by its namespace)")
            return True

        print("=" * 70)
        print(f"Migration {MIGRATION_NUMBER}: Repositories belong to their namespace")
        print("=" * 70)
        with db.atomic():
            _repair()
        print(f"Migration {MIGRATION_NUMBER}: ✓ Completed Successfully")
        return True

    except Exception as e:
        print(f"\n✗ Migration {MIGRATION_NUMBER} failed: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    run()
