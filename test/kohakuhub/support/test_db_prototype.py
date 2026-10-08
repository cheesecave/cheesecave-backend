"""Prototype experiments for the shared database fixture plan (see the planning issue)."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from kohakuhub.db import Repository, User
from test.kohakuhub.support.db import MODELS, fresh_database, make_database, rolled_back

_SHARED = {}


@pytest.fixture(scope="module")
def shared_db(tmp_path_factory):
    database, schema = make_database(tmp_path_factory.mktemp("shared"), name="shared")
    cm = fresh_database(database, MODELS, schema=schema)
    db = cm.__enter__()
    _SHARED["cm"] = cm
    yield db
    cm.__exit__(None, None, None)
    database.close()


@pytest.fixture
def scoped(shared_db):
    with rolled_back(shared_db):
        yield shared_db


def _owner(db):
    return User.get_or_none(User.username == "owner") or User.create(
        username="owner", normalized_name="owner", email="owner@example.com"
    )


@pytest.mark.parametrize("attempt", range(3))
def test_writes_do_not_leak_between_tests_writer(scoped, attempt):
    owner = _owner(scoped)
    Repository.create(repo_type="model", namespace="owner", name=f"r{attempt}", full_id=f"owner/r{attempt}", owner=owner, created_at=datetime(2025, 1, 1))
    assert Repository.select().count() == 1


def test_previous_writes_are_gone_in_the_next_test(scoped):
    assert Repository.select().count() == 0
    assert User.select().count() == 0


def test_bindings_are_restored_after_the_fresh_scope(tmp_path):
    before = {m: m._meta.database for m in MODELS}
    database, schema = make_database(tmp_path, name="restore")
    with fresh_database(database, MODELS, schema=schema):
        assert Repository._meta.database is database
    database.close()
    assert all(m._meta.database is before[m] for m in MODELS)


@pytest.mark.parametrize("rows", [40])
def test_speed_rolled_back_scope(scoped, rows):
    owner = _owner(scoped)
    start = time.perf_counter()
    for i in range(rows):
        Repository.create(repo_type="model", namespace="owner", name=f"s{i}", full_id=f"owner/s{i}", owner=owner, created_at=datetime(2025, 1, 1))
    assert Repository.select().count() == rows
    _SHARED.setdefault("rolled", []).append(time.perf_counter() - start)


def test_speed_fresh_scope(tmp_path):
    database, schema = make_database(tmp_path, name="speed")
    start = time.perf_counter()
    with fresh_database(database, MODELS, schema=schema) as db:
        owner = User.create(username="owner", normalized_name="owner", email="owner@example.com")
        for i in range(40):
            Repository.create(repo_type="model", namespace="owner", name=f"s{i}", full_id=f"owner/s{i}", owner=owner, created_at=datetime(2025, 1, 1))
        assert Repository.select().count() == 40
    database.close()
    _SHARED.setdefault("fresh", []).append(time.perf_counter() - start)
