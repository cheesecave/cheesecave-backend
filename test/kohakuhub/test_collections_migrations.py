"""Migration 042 builds the three collection tables, the way the models declare them.

Tables of their own: a release from before it keeps running on the same database and ignores them. Nothing is backfilled.
Each test starts from ``db_dual`` (SQLite and PostgreSQL) holding the schema 041 leaves.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from kohakuhub.db import Collection, CollectionItem, CollectionUpvote, Repository, User

MIGRATION = Path(__file__).resolve().parents[2] / "scripts/db_migrations/042_collections.py"
TABLES = ("collection", "collectionitem", "collectionupvote")


@pytest.fixture
def migration(db_dual, monkeypatch):
    spec = importlib.util.spec_from_file_location("collections_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = "sqlite" if isinstance(db_dual, SqliteDatabase) else "postgres"
    monkeypatch.setattr(module, "db", db_dual)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    db_dual.drop_tables([CollectionUpvote, CollectionItem, Collection], safe=True)    # the schema before 042
    return module


def _rows(db):
    owner = User.create(username="owner", normalized_name="owner")
    repo = Repository.create(repo_type="model", namespace="owner", name="m", full_id="owner/m", owner=owner)
    first = Collection.create(oid="a" * 24, owner=owner, title="One", slug="one", theme="green")
    second = Collection.create(oid="b" * 24, owner=owner, title="Two", slug="two", theme="blue")
    return owner, repo, first, second


def _rollback(db):
    if not isinstance(db, SqliteDatabase):
        db.rollback()


def test_it_creates_the_tables_the_models_declare(migration, db_dual):
    assert not any(db_dual.table_exists(t) for t in TABLES)

    assert migration.run() is True

    columns = {t: {c.name: c for c in db_dual.get_columns(t)} for t in TABLES}
    assert set(columns["collection"]) == {
        "id", "oid", "owner_id", "title", "slug", "description", "theme", "private", "position", "upvotes_count", "created_at", "updated_at",
    }
    assert set(columns["collectionitem"]) == {"id", "oid", "collection_id", "item_type", "repository_id", "child_id", "position", "note", "created_at"}
    assert set(columns["collectionupvote"]) == {"id", "collection_id", "user_id", "created_at"}
    assert columns["collection"]["description"].null and not columns["collection"]["title"].null
    assert columns["collectionitem"]["repository_id"].null and columns["collectionitem"]["child_id"].null and columns["collectionitem"]["note"].null


def test_the_tables_do_what_the_models_need(migration, db_dual):
    migration.run()
    owner, repo, first, second = _rows(db_dual)

    with pytest.raises(IntegrityError):                       # one title slug per owner
        Collection.create(oid="c" * 24, owner=owner, title="Other", slug="one", theme="green")
    _rollback(db_dual)
    with pytest.raises(IntegrityError):                       # the oid is the address
        Collection.create(oid="a" * 24, owner=owner, title="Other", slug="other", theme="green")
    _rollback(db_dual)

    CollectionItem.create(oid="1" * 24, collection=first, item_type="model", repository=repo, position=0)
    CollectionItem.create(oid="2" * 24, collection=first, item_type="collection", child=second, position=1)
    for values in ({"repository": repo}, {"child": second}):  # an item is in a collection once
        with pytest.raises(IntegrityError):
            CollectionItem.create(oid="3" * 24, collection=first, item_type="x", position=2, **values)
        _rollback(db_dual)
    for values in ({}, {"repository": repo, "child": second}):   # a repository or a collection, never both, never neither
        with pytest.raises(IntegrityError):
            CollectionItem.create(oid="4" * 24, collection=second, item_type="x", position=0, **values)
        _rollback(db_dual)

    CollectionUpvote.create(collection=first, user=owner)
    with pytest.raises(IntegrityError):                       # one upvote per user
        CollectionUpvote.create(collection=first, user=owner)
    _rollback(db_dual)


def test_rows_leave_with_what_they_belong_to(migration, db_dual):
    migration.run()
    owner, repo, first, second = _rows(db_dual)
    CollectionItem.create(oid="1" * 24, collection=first, item_type="model", repository=repo, position=0)
    CollectionItem.create(oid="2" * 24, collection=first, item_type="collection", child=second, position=1)
    CollectionUpvote.create(collection=second, user=owner)

    Repository.delete().where(Repository.id == repo.id).execute()          # its items leave
    assert [i.oid for i in CollectionItem.select()] == ["2" * 24]
    Collection.delete().where(Collection.id == second.id).execute()       # the references to it, and its upvotes, leave
    assert (CollectionItem.select().count(), CollectionUpvote.select().count()) == (0, 0)
    User.delete().where(User.id == owner.id).execute()                    # collections go with their owner
    assert Collection.select().count() == 0


def test_it_builds_the_indexes_the_models_declare(migration, db_dual):
    migration.run()

    names = {i.name for t in TABLES for i in db_dual.get_indexes(t)}

    assert {
        "collection_oid", "collection_owner_id", "collection_slug", "collection_private", "collection_updated_at", "collection_owner_id_slug",
        "collectionitem_oid", "collectionitem_collection_id", "collectionitem_repository_id", "collectionitem_child_id",
        "collectionitem_collection_id_repository_id", "collectionitem_collection_id_child_id",
        "collectionupvote_collection_id", "collectionupvote_user_id", "collectionupvote_created_at", "collectionupvote_collection_id_user_id",
    } <= names


def test_a_second_run_is_a_no_op(migration, db_dual):
    assert migration.run() is True
    assert migration.is_applied(db_dual, migration.cfg)
    _rows(db_dual)

    assert migration.run() is True
    assert Collection.select().count() == 2


def test_it_is_not_applied_while_one_table_is_missing(migration, db_dual):
    migration.run()
    db_dual.drop_tables([CollectionUpvote], safe=True)

    assert not migration.is_applied(db_dual, migration.cfg)
    assert migration.run() is True and db_dual.table_exists("collectionupvote")


def test_init_db_builds_no_second_set_of_tables(migration, db_dual):
    migration.run()

    db_dual.create_tables([Collection, CollectionItem, CollectionUpvote], safe=True)  # what init_db() does at boot

    assert all(db_dual.table_exists(t) for t in TABLES)


def test_it_skips_when_a_later_migration_is_applied(migration, monkeypatch):
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *a: True)

    assert migration.run() is True
    assert not migration.db.table_exists("collection")


def test_it_fails_loudly_when_the_database_refuses(migration, monkeypatch):
    monkeypatch.setattr(migration, "_migrate", lambda: (_ for _ in ()).throw(RuntimeError("no")))

    assert migration.run() is False
