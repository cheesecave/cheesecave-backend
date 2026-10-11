"""Issue #11 (expand 032, then 033 drops the old key): File rows gain a branch.

Each test runs on a new ``db_dual`` database (SQLite and PostgreSQL). The pre-A
``file`` table is rebuilt as raw DDL, matching what peewee generated before this
change, so the expand is checked against the schema production has. Nothing here
runs the migration runner: the migration modules are loaded and bound to the test
database with ``monkeypatch``.
"""

import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from peewee import EXCLUDED, IntegrityError, SqliteDatabase
import pytest

from kohakuhub.db import File
from test.kohakuhub.support import factories

ROOT = Path(__file__).resolve().parents[2]
EXPAND_PATH = ROOT / "scripts/db_migrations/032_file_branch_expand.py"
DROP_PATH = ROOT / "scripts/db_migrations/033_file_branch_drop_old_key.py"
NEW_INDEX = "file_repository_id_branch_path_in_repo"
OLD_INDEX = "file_repository_id_path_in_repo"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _postgres(database):
    return not isinstance(database, SqliteDatabase)


def _sql(database, statement):
    """Peewee's raw SQL uses ``%s`` on PostgreSQL and ``?`` on SQLite."""
    return statement if _postgres(database) else statement.replace("%s", "?")


def _bind(monkeypatch, database, module):
    """Point a migration module at the test's database and its backend."""
    backend = "postgres" if _postgres(database) else "sqlite"
    monkeypatch.setattr(module, "db", database)
    monkeypatch.setattr(module, "cfg", SimpleNamespace(app=SimpleNamespace(db_backend=backend)))
    return module


def _old_file_table(database):
    """Rebuild ``file`` as it was before issue #11: no branch, only the old key.

    ``lfsobjecthistory`` references ``file``; CASCADE removes that foreign key with
    the table, and these tests never read it.
    """
    database.execute_sql('DROP TABLE "file" CASCADE' if _postgres(database) else 'DROP TABLE "file"')
    id_column = (
        '"id" SERIAL NOT NULL PRIMARY KEY' if _postgres(database) else '"id" INTEGER NOT NULL PRIMARY KEY'
    )
    database.execute_sql(
        f'CREATE TABLE "file" ({id_column}, "repository_id" INTEGER NOT NULL, '
        '"path_in_repo" TEXT NOT NULL, "size" BIGINT NOT NULL, "sha256" VARCHAR(255) NOT NULL, '
        '"lfs" BOOLEAN NOT NULL, "is_deleted" BOOLEAN NOT NULL, "owner_id" INTEGER NOT NULL, '
        '"created_at" TIMESTAMP NOT NULL, "updated_at" TIMESTAMP NOT NULL, '
        'FOREIGN KEY ("repository_id") REFERENCES "repository" ("id") ON DELETE CASCADE, '
        'FOREIGN KEY ("owner_id") REFERENCES "user" ("id") ON DELETE CASCADE)'
    )
    database.execute_sql('CREATE INDEX "file_repository_id" ON "file" ("repository_id")')
    database.execute_sql(f'CREATE UNIQUE INDEX "{OLD_INDEX}" ON "file" ("repository_id", "path_in_repo")')
    database.execute_sql('DROP TABLE IF EXISTS "schema_rollout"')


def _index_names(database):
    return {index.name for index in database.get_indexes("file")}


def _columns(database):
    return {column.name for column in database.get_columns("file")}


def _old_code_upsert(database, repo, path, sha):
    """The pre-A writer's statement: conflict on (repository, path), no branch column."""
    database.execute_sql(
        _sql(
            database,
            'INSERT INTO "file" ("repository_id", "path_in_repo", "size", "sha256", "lfs", '
            '"is_deleted", "owner_id", "created_at", "updated_at") '
            "VALUES (%s, %s, 1, %s, FALSE, FALSE, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            'ON CONFLICT ("repository_id", "path_in_repo") DO UPDATE SET "sha256" = EXCLUDED."sha256"',
        ),
        (repo.id, path, sha, repo.owner_id),
    )


def _new_upsert(repo, path, sha):
    """The stage-2 writer's shape: conflict on (repository, branch, path)."""
    File.insert_many(
        [
            {
                "repository": repo,
                "branch": "main",
                "path_in_repo": path,
                "size": 1,
                "sha256": sha,
                "lfs": False,
                "is_deleted": False,
                "owner_id": repo.owner_id,
            }
        ]
    ).on_conflict(
        conflict_target=(File.repository, File.branch, File.path_in_repo),
        update={File.sha256: EXCLUDED.sha256},
    ).execute()


@pytest.fixture
def pre_a(db_dual, monkeypatch):
    """The expand module bound to a pre-A database, with one repository and one main row."""
    _old_file_table(db_dual)
    module = _bind(monkeypatch, db_dual, _load("file_branch_expand", EXPAND_PATH))
    owner = factories.make_user("owner")
    repo = factories.make_repo(owner, "repo")
    _old_code_upsert(db_dual, repo, "kept.txt", "b" * 64)
    return module, db_dual, repo


@pytest.fixture
def expanded(pre_a):
    module, database, repo = pre_a
    assert module.run() is True
    return module, database, repo


@pytest.fixture
def drop(expanded, monkeypatch):
    """Migration 033 bound to the expanded database (the old key still there)."""
    _expand_module, database, repo = expanded
    module = _bind(monkeypatch, database, _load("file_branch_drop_old_key", DROP_PATH))
    return module, database, repo


# --- fresh databases --------------------------------------------------------------


def test_fresh_database_has_the_branch_column_and_only_the_new_key(db_dual):
    """A database built from the models has the final shape: no pre-A key (S2 change)."""
    assert "branch" in _columns(db_dual)
    assert NEW_INDEX in _index_names(db_dual)
    assert OLD_INDEX not in _index_names(db_dual)


def test_fresh_rows_default_to_main(db_dual):
    repo = factories.make_repo(factories.make_user("owner"), "repo")
    row = factories.make_file(repo, "a.bin", "c" * 64)
    assert File.get_by_id(row.id).branch == "main"


# --- expand ------------------------------------------------------------------------


def test_expand_is_not_applied_before_it_runs(pre_a):
    module, database, _repo = pre_a
    assert not module.is_applied(database, module.cfg)
    assert "branch" not in _columns(database)


def test_expand_adds_the_branch_column_the_new_key_and_the_rollout_table(pre_a):
    module, database, _repo = pre_a
    assert module.run() is True
    assert module.is_applied(database, module.cfg)
    assert "branch" in _columns(database)
    assert {NEW_INDEX, OLD_INDEX} <= _index_names(database)
    assert "schema_rollout" in set(database.get_tables())
    rows = database.execute_sql('SELECT "path_in_repo", "branch" FROM "file"').fetchall()
    assert rows == [("kept.txt", "main")]  # existing rows become main


def test_expand_is_idempotent(expanded):
    module, database, _repo = expanded
    before = _index_names(database)
    assert module.run() is True
    assert _index_names(database) == before


def test_old_code_path_writes_main_during_expand(expanded):
    """Old replicas keep working: they conflict on the old key and write branch 'main'."""
    _module, database, repo = expanded
    _old_code_upsert(database, repo, "kept.txt", "d" * 64)  # conflicts on the old key
    _old_code_upsert(database, repo, "new.txt", "e" * 64)  # inserts
    rows = dict(database.execute_sql('SELECT "path_in_repo", "sha256" FROM "file"').fetchall())
    assert rows == {"kept.txt": "d" * 64, "new.txt": "e" * 64}
    branches = {row[0] for row in database.execute_sql('SELECT "branch" FROM "file"').fetchall()}
    assert branches == {"main"}


def test_new_code_path_upserts_on_the_new_key_during_expand(expanded):
    _module, database, repo = expanded
    _new_upsert(repo, "kept.txt", "1" * 64)
    _new_upsert(repo, "kept.txt", "2" * 64)  # conflicts on (repository, branch, path)
    rows = database.execute_sql('SELECT "path_in_repo", "sha256", "branch" FROM "file"').fetchall()
    assert rows == [("kept.txt", "2" * 64, "main")]


def test_non_main_row_for_a_path_main_has_is_refused_during_expand(expanded):
    """The guard: the old key still covers (repository, path), so a branch copy of a
    path main already has cannot be written while the old key exists."""
    _module, database, repo = expanded
    with pytest.raises(IntegrityError):
        with database.atomic():
            File.create(
                repository=repo,
                branch="dev",
                path_in_repo="kept.txt",
                size=1,
                sha256="3" * 64,
                owner_id=repo.owner_id,
            )
    branches = [row[0] for row in database.execute_sql('SELECT "branch" FROM "file"').fetchall()]
    assert branches == ["main"]


def test_non_main_row_for_a_path_main_lacks_is_allowed_during_expand(expanded):
    """Known limit of the expanded state: the old key does not cover a path main lacks.
    The row written is correct (branch = dev); the refusal above guards shared paths only.
    The code that writes non-main rows relies on 033 having run."""
    _module, database, repo = expanded
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="dev-only.txt",
        size=1,
        sha256="4" * 64,
        owner_id=repo.owner_id,
    )
    assert File.select().where(File.branch == "dev").count() == 1


# --- rollback ----------------------------------------------------------------------


def test_rollback_refuses_while_a_non_main_row_exists(expanded):
    module, database, repo = expanded
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="dev-only.txt",
        size=1,
        sha256="5" * 64,
        owner_id=repo.owner_id,
    )
    with pytest.raises(RuntimeError, match="1 file row") as refused:
        module.rollback(database, module.cfg)
    assert "docs/deployment/production.md" in str(refused.value)  # where an operator looks next
    assert "branch" in _columns(database)  # nothing changed


def test_rollback_returns_to_the_pre_a_schema_when_only_main_rows_exist(expanded):
    module, database, repo = expanded
    module.rollback(database, module.cfg)
    assert "branch" not in _columns(database)
    assert OLD_INDEX in _index_names(database)
    assert NEW_INDEX not in _index_names(database)
    assert "schema_rollout" not in set(database.get_tables())
    assert not module.is_applied(database, module.cfg)
    _old_code_upsert(database, repo, "kept.txt", "6" * 64)  # the pre-A writer works again


def test_rollback_without_the_column_does_nothing(expanded):
    module, database, _repo = expanded
    module.rollback(database, module.cfg)
    module.rollback(database, module.cfg)  # second call: already rolled back
    assert "branch" not in _columns(database)


def test_rollback_restores_the_old_key_033_dropped(expanded, drop):
    expand_module, _database, _repo = expanded
    module, database, _repo = drop
    assert module.run() is True
    # 033 keeps the name for its guard; the unique key is what it drops
    assert _unique_pre_a_key(database) == []
    expand_module.rollback(database, expand_module.cfg)
    assert OLD_INDEX in _index_names(database)
    assert len(_unique_pre_a_key(database)) == 1


# --- 033: the old key stops being unique -----------------------------------------------


def _unique_old_key(database):
    return _unique_pre_a_key(database)


def _guard(database):
    """The guard: the old name taken by a non-unique index."""
    return [index for index in database.get_indexes("file") if index.name == OLD_INDEX and not index.unique]


def _other_connection(database):
    other = type(database)(database.database, **database.connect_params)
    other.connect()
    return other


def _fast(monkeypatch, module, lock_timeout_ms=100, backoff=(0, 0)):
    """Short timeouts, and a recording ``sleep`` so retries cost no time."""
    sleeps = []
    monkeypatch.setattr(module, "LOCK_TIMEOUT_MS", lock_timeout_ms)
    monkeypatch.setattr(module, "BACKOFF_SECONDS", backoff)
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=sleeps.append))
    return sleeps


def test_033_is_not_applied_while_the_old_key_exists(drop):
    module, database, _repo = drop
    assert len(_unique_old_key(database)) == 1
    assert not module.is_applied(database, module.cfg)


def test_033_drops_the_old_key_keeps_the_guard_and_the_rows(drop, capsys):
    module, database, repo = drop
    assert module.run() is True
    assert "Completed" in capsys.readouterr().out
    assert module.is_applied(database, module.cfg)
    assert _unique_old_key(database) == []
    assert len(_guard(database)) == 1  # the name stays taken (D1)
    assert NEW_INDEX in _index_names(database)
    assert database.execute_sql('SELECT "path_in_repo", "branch" FROM "file"').fetchall() == [("kept.txt", "main")]
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="kept.txt",  # a path main has: refused while the old key existed
        size=1,
        sha256="7" * 64,
        owner_id=repo.owner_id,
    )
    assert File.select().where(File.branch == "dev").count() == 1


def test_033_does_not_leak_its_timeouts_into_the_session(drop):
    module, database, _repo = drop
    assert module.run() is True
    if _postgres(database):
        assert database.execute_sql("SHOW lock_timeout").fetchone()[0] == "0"
        assert database.execute_sql("SHOW statement_timeout").fetchone()[0] == "0"


def test_033_running_again_is_a_no_op(drop, capsys):
    module, database, _repo = drop
    assert module.run() is True
    before = _index_names(database)
    capsys.readouterr()
    assert module.run() is True
    assert "Already applied" in capsys.readouterr().out
    assert _index_names(database) == before


def test_033_on_a_fresh_database_is_a_no_op(db_dual, monkeypatch, capsys):
    """A database built by init_db never had the old key: nothing to drop, nothing created."""
    module = _bind(monkeypatch, db_dual, _load("file_branch_drop_old_key", DROP_PATH))
    before = _index_names(db_dual)
    assert module.is_applied(db_dual, module.cfg)
    assert module.run() is True
    assert "Already applied" in capsys.readouterr().out
    assert _index_names(db_dual) == before
    assert OLD_INDEX not in _index_names(db_dual)


def test_033_is_not_applied_on_a_pre_a_database_even_without_the_old_key(pre_a, monkeypatch):
    """No branch column means 032 has not run: earlier migrations must not skip on 033's account."""
    _expand, database, _repo = pre_a
    module = _bind(monkeypatch, database, _load("file_branch_drop_old_key", DROP_PATH))
    assert not module.is_applied(database, module.cfg)  # old key present
    database.execute_sql(f'DROP INDEX "{OLD_INDEX}"')
    assert not module.is_applied(database, module.cfg)  # no key, still no branch column


def test_033_is_skipped_when_a_future_migration_is_applied(drop, monkeypatch, capsys):
    module, database, _repo = drop
    monkeypatch.setattr(module, "should_skip_due_to_future_migrations", lambda *args: True)
    assert module.run() is True
    assert "Skipped" in capsys.readouterr().out
    assert len(_unique_old_key(database)) == 1


def test_a_database_without_032_goes_through_032_then_033_in_one_run(pre_a, monkeypatch):
    """The path of anyone who skipped the previous deploy: the runner applies 032, then 033."""
    runner = _load("run_migrations_order", ROOT / "scripts/run_migrations.py")
    names = [name for name, _path in runner.discover_migrations()]
    assert names[-2:] == ["032_file_branch_expand", "033_file_branch_drop_old_key"]

    expand_module, database, repo = pre_a
    module = _bind(monkeypatch, database, _load("file_branch_drop_old_key", DROP_PATH))
    assert expand_module.run() is True
    assert len(_unique_old_key(database)) == 1  # 032 keeps the old key
    assert module.run() is True
    assert _unique_old_key(database) == []
    assert "branch" in _columns(database)
    assert database.execute_sql('SELECT COUNT(*) FROM "file"').fetchone()[0] == 1


def test_033_drops_an_old_key_that_has_another_name(drop):
    module, database, _repo = drop
    database.execute_sql(f'DROP INDEX "{OLD_INDEX}"')
    database.execute_sql('CREATE UNIQUE INDEX "file_legacy_key" ON "file" ("repository_id", "path_in_repo")')
    assert module.run() is True
    assert _unique_old_key(database) == []
    assert "file_legacy_key" not in _index_names(database)
    assert len(_guard(database)) == 1


def test_033_drops_an_old_key_that_is_a_unique_constraint(drop):
    module, database, _repo = drop
    if not _postgres(database):
        pytest.skip("constraints that own an index are PostgreSQL's")
    database.execute_sql(f'DROP INDEX "{OLD_INDEX}"')
    database.execute_sql(
        'ALTER TABLE "file" ADD CONSTRAINT "file_legacy_unique" UNIQUE ("repository_id", "path_in_repo")'
    )
    assert module.run() is True
    assert _unique_old_key(database) == []
    assert len(_guard(database)) == 1


def test_033_leaves_other_unique_indexes_alone(drop):
    module, database, _repo = drop
    database.execute_sql('CREATE UNIQUE INDEX "file_other" ON "file" ("path_in_repo", "repository_id")')
    assert module.run() is True
    assert "file_other" in _index_names(database)


def test_033_reports_an_error_that_is_not_a_lock_timeout_without_retrying(drop, monkeypatch, capsys):
    module, database, _repo = drop
    sleeps = _fast(monkeypatch, module)

    def explode(database, config):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "drop_old_key", explode)
    assert module.run() is False
    assert "Migration 33 failed: boom" in capsys.readouterr().err
    assert sleeps == []


# --- 033 never leaves the table without a unique key -------------------------------------


def test_033_refuses_on_a_database_without_032(pre_a, monkeypatch, capsys):
    """The runner goes on after a failed migration: 033 must not drop the only unique key."""
    _expand, database, _repo = pre_a
    module = _bind(monkeypatch, database, _load("file_branch_drop_old_key", DROP_PATH))
    assert module.run() is False
    assert "per-branch unique key" in capsys.readouterr().err
    assert len(_unique_old_key(database)) == 1
    assert not _guard(database)


def test_033_refuses_when_the_per_branch_key_is_missing(drop, capsys):
    module, database, _repo = drop
    if _postgres(database):
        database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')
    else:
        database.execute_sql(f'DROP INDEX "{NEW_INDEX}"')
    assert module.run() is False
    assert "missing or invalid" in capsys.readouterr().err
    assert len(_unique_old_key(database)) == 1


def test_033_refuses_when_the_per_branch_key_is_invalid(drop, capsys):
    """Made invalid for real in the catalog (superuser), as a crashed CONCURRENTLY build leaves it."""
    module, database, _repo = drop
    if not _postgres(database):
        pytest.skip("index validity is PostgreSQL's")
    if not database.execute_sql("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
        pytest.skip("marking an index invalid needs superuser")
    database.execute_sql(
        "UPDATE pg_index SET indisvalid = FALSE WHERE indexrelid = "
        f"(SELECT oid FROM pg_class WHERE relname = '{NEW_INDEX}' "
        "AND relnamespace = to_regnamespace(current_schema()))"
    )
    assert module.run() is False
    assert "missing or invalid" in capsys.readouterr().err
    assert len(_unique_old_key(database)) == 1


# --- 033: every starting point ends in the same schema ------------------------------------


def _file_shape(database):
    """The ``file`` table as the application sees it: columns, and each unique key's columns."""
    return (
        sorted(_columns(database)),
        sorted(tuple(index.columns) for index in database.get_indexes("file") if index.unique),
    )


def test_pre_a_032_only_and_fresh_databases_end_in_the_same_file_schema(
    db_backend, tmp_path, monkeypatch
):
    """A pre-A database through 032 and 033 in one run, a 032-only database (production's
    state) through 033, and a database built by init_db all end with the same ``file`` columns
    and the same unique keys. The old name is taken by the guard on the first two only."""
    from test.kohakuhub.support import db as support

    shapes = {}
    guards = {}
    for name in ("one_run", "expanded_first", "fresh"):
        database, schema = support.make_database(tmp_path, name=name, backend=db_backend)
        with support.fresh_database(database, support.MODELS, schema=schema):
            if name != "fresh":
                _old_file_table(database)
                expand = _bind(monkeypatch, database, _load("file_branch_expand", EXPAND_PATH))
                drop = _bind(monkeypatch, database, _load("file_branch_drop_old_key", DROP_PATH))
                assert expand.run() is True
                if name == "expanded_first":
                    assert len(_unique_old_key(database)) == 1  # production's state
                assert drop.run() is True
            shapes[name] = _file_shape(database)
            guards[name] = bool(_guard(database))
        database.close()
    assert shapes["one_run"] == shapes["expanded_first"] == shapes["fresh"]
    assert ("repository_id", "branch", "path_in_repo") in shapes["fresh"][1]
    assert ("repository_id", "path_in_repo") not in shapes["fresh"][1]
    assert guards == {"one_run": True, "expanded_first": True, "fresh": False}


# --- 033: locks ---------------------------------------------------------------------


@pytest.fixture
def holder(drop):
    """A second connection with an open transaction that has read ``file``: a long-running
    reader. It conflicts with the ACCESS EXCLUSIVE lock the DDL needs. Released on teardown."""
    module, database, _repo = drop
    if not _postgres(database):
        pytest.skip("lock_timeout is PostgreSQL's; SQLite has its own test")
    other = _other_connection(database)
    other.execute_sql("BEGIN")
    other.execute_sql('SELECT COUNT(*) FROM "file"')
    yield SimpleNamespace(module=module, database=database, other=other)
    other.execute_sql("ROLLBACK")
    other.close()


def test_033_gives_up_after_bounded_retries_with_a_specific_error(holder, monkeypatch, capsys):
    module = holder.module
    sleeps = _fast(monkeypatch, module, lock_timeout_ms=100, backoff=(1, 2))
    started = time.monotonic()
    assert module.run() is False
    assert time.monotonic() - started < 10  # bounded, no hang
    err = capsys.readouterr().err
    assert "was not free in 3 attempts of 100 ms" in err
    assert "pg_stat_activity" in err and "restart the container" in err
    assert sleeps == [1, 2]  # the backoff between the three attempts
    assert len(_unique_old_key(holder.database)) == 1  # nothing changed


def test_033_completes_when_the_lock_is_freed_between_attempts(holder, monkeypatch, capsys):
    module = holder.module
    sleeps = _fast(monkeypatch, module, backoff=(0, 0))
    monkeypatch.setattr(
        module, "time", SimpleNamespace(sleep=lambda seconds: (sleeps.append(seconds), holder.other.execute_sql("ROLLBACK")))
    )
    assert module.run() is True
    out = capsys.readouterr().out
    assert "file is locked, retrying in 0 s" in out and "Completed" in out
    assert sleeps == [0]  # attempt 2 got the lock
    assert _unique_old_key(holder.database) == []


def test_a_writer_queued_behind_033_waits_at_most_the_lock_timeout(holder, monkeypatch):
    """The point of lock_timeout: the DDL's queued lock request blocks later writers, but only
    until it gives up, not until the long transaction ends."""
    module = holder.module
    _fast(monkeypatch, module, lock_timeout_ms=1500, backoff=())
    results = []
    migration = threading.Thread(target=lambda: results.append(module.run()))
    migration.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # the request is queued once pg_locks has it ungranted
            waiting = holder.database.execute_sql(
                "SELECT COUNT(*) FROM pg_locks WHERE NOT granted AND mode = 'AccessExclusiveLock'"
            ).fetchone()[0]
            if waiting:
                break
            time.sleep(0.02)
        assert waiting
        started = time.monotonic()
        writer = _other_connection(holder.database)
        writer.execute_sql('SELECT COUNT(*) FROM "file"')  # blocked behind the queued request
        waited = time.monotonic() - started
        writer.close()
    finally:
        migration.join(timeout=10)
    assert results == [False]
    assert waited < 1.5 + 1  # released when the DDL's request timed out
    assert holder.other.execute_sql('SELECT COUNT(*) FROM "file"').fetchone()[0] == 1  # the reader lived on


def _backends_waiting_for_a_lock(database):
    return database.execute_sql("SELECT COUNT(*) FROM pg_locks WHERE NOT granted").fetchone()[0]


def test_033_run_after_another_run_took_the_lock_is_a_no_op(drop, monkeypatch, capsys):
    """Two containers: B has seen the old key and waits for the lock while A drops it. After A
    commits, B finds it gone under the lock and does nothing."""
    module, database, _repo = drop
    if not _postgres(database):
        pytest.skip("lock queues are PostgreSQL's")
    monkeypatch.setattr(module, "LOCK_TIMEOUT_MS", 20000)
    first = _other_connection(database)
    first.execute_sql("BEGIN")
    first.execute_sql('LOCK TABLE "file" IN ACCESS EXCLUSIVE MODE')
    results = []
    second = threading.Thread(target=lambda: results.append(module.run()))
    # B's pre-check runs before A changes anything: it sees the old key and queues for the lock
    second.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _backends_waiting_for_a_lock(database):
            time.sleep(0.02)
        assert _backends_waiting_for_a_lock(database)
        first.execute_sql(f'DROP INDEX "{OLD_INDEX}"')
        first.execute_sql(
            f'CREATE INDEX "{OLD_INDEX}" ON "file" ("repository_id", "path_in_repo") WHERE FALSE'
        )
        first.execute_sql("COMMIT")
    finally:
        second.join(timeout=30)
        first.close()
    assert results == [True]
    assert "Already applied (by another run)" in capsys.readouterr().out
    assert _unique_old_key(database) == []
    assert len(_guard(database)) == 1


def test_033_started_twice_at_once_drops_the_key_exactly_once(drop, capfd):
    module, database, _repo = drop
    barrier = threading.Barrier(2)
    results = []

    def start():
        barrier.wait()
        results.append(module.run())

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    captured = capfd.readouterr()
    assert results == [True, True]
    assert captured.out.count("✓ Completed") == 1
    assert captured.out.count("Already applied") == 1
    assert captured.err == ""
    assert _unique_old_key(database) == []
    assert len(_guard(database)) == 1


def test_033_on_sqlite_gives_up_when_the_database_stays_locked(drop, monkeypatch, capsys):
    module, database, _repo = drop
    if _postgres(database):
        pytest.skip("SQLite's busy timeout")
    import sqlite3

    sleeps = _fast(monkeypatch, module, backoff=(0,))
    database.execute_sql("PRAGMA busy_timeout = 50")
    blocker = sqlite3.connect(database.database, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        assert module.run() is False
        assert "was not free in 2 attempts" in capsys.readouterr().err
        assert sleeps == [0]
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert module.run() is True  # restart after the lock is gone
    assert _unique_old_key(database) == []

# --- runner entry points -----------------------------------------------------------


def test_expand_reports_a_failure_instead_of_raising(pre_a, monkeypatch):
    module, _database, _repo = pre_a

    def explode(database, config):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "expand", explode)
    assert module.run() is False


def test_expand_skips_when_a_future_migration_is_applied(pre_a, monkeypatch):
    module, database, _repo = pre_a
    monkeypatch.setattr(module, "should_skip_due_to_future_migrations", lambda *args: True)
    assert module.run() is True
    assert not module.is_applied(database, module.cfg)


def test_rollback_on_sqlite_recreates_the_old_key(expanded):
    module, database, _repo = expanded
    if _postgres(database):
        pytest.skip("SQLite-only branch of rollback")
    module.rollback(database, module.cfg)
    assert OLD_INDEX in _index_names(database)


def test_rollback_on_postgres_detaches_the_constraint_first(expanded):
    module, database, _repo = expanded
    if not _postgres(database):
        pytest.skip("the constraint is PostgreSQL-only")

    def constraints():
        return database.execute_sql(
            "SELECT COUNT(*) FROM pg_constraint WHERE conname = %s", (NEW_INDEX,)
        ).fetchone()[0]

    assert constraints() == 1
    module.rollback(database, module.cfg)
    assert constraints() == 0


def test_expand_rebuilds_an_invalid_new_index(expanded):
    """A crashed CONCURRENTLY build leaves an invalid index. The next run replaces it.

    The invalid state is made for real in the catalog (indisvalid = false), which needs
    superuser; the test is skipped otherwise.
    """
    module, database, _repo = expanded
    if not _postgres(database):
        pytest.skip("CONCURRENTLY is PostgreSQL-only")
    is_super = database.execute_sql(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()[0]
    if not is_super:
        pytest.skip("marking an index invalid needs superuser")
    database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')  # drops the index too
    database.execute_sql(f'CREATE UNIQUE INDEX "{NEW_INDEX}" ON "file" ("repository_id", "branch", "path_in_repo")')
    database.execute_sql(
        "UPDATE pg_index SET indisvalid = FALSE WHERE indexrelid = "
        f"(SELECT oid FROM pg_class WHERE relname = '{NEW_INDEX}' "
        "AND relnamespace = to_regnamespace(current_schema()))"
    )
    assert not module.is_applied(database, module.cfg)
    assert module.run() is True
    assert module.is_applied(database, module.cfg)


# --- partial runs: a crashed expand is finished by the next run -------------------


def test_expand_finishes_a_run_that_stopped_after_the_index(expanded):
    """Stopped after the index and the constraint, before the rollout table: the next run
    finds the column, the index and the constraint in place and only creates the table."""
    module, database, _repo = expanded
    database.execute_sql('DROP TABLE "schema_rollout"')
    assert not module.is_applied(database, module.cfg)
    assert module.run() is True
    assert module.is_applied(database, module.cfg)
    assert NEW_INDEX in _index_names(database)


def test_expand_finishes_a_run_that_stopped_after_the_column(pre_a):
    """Stopped after the column and the index: no constraint and no rollout table yet."""
    module, database, _repo = pre_a
    database.execute_sql('ALTER TABLE "file" ADD COLUMN "branch" VARCHAR(255) NOT NULL DEFAULT \'main\'')
    database.execute_sql(
        f'CREATE UNIQUE INDEX "{NEW_INDEX}" ON "file" ("repository_id", "branch", "path_in_repo")'
    )
    assert module.run() is True
    assert module.is_applied(database, module.cfg)


def test_rollback_when_the_constraint_is_already_gone(expanded):
    """Postgres only: the constraint owns the new index, so dropping it removes the index too."""
    module, database, repo = expanded
    if not _postgres(database):
        pytest.skip("the constraint is PostgreSQL-only")
    database.execute_sql(f'ALTER TABLE "file" DROP CONSTRAINT "{NEW_INDEX}"')
    module.rollback(database, module.cfg)
    assert "branch" not in _columns(database)
    assert OLD_INDEX in _index_names(database)


# --- D1: a pre-A image's boot after 033 --------------------------------------


def _pre_a_file_model(database):
    """The File model as the code before issue #11 declared it: a unique key on
    (repository, path_in_repo) and nothing else. Its ``create_tables`` is what
    an old replica runs at boot (``crud.py`` calls ``init_db()``)."""
    from peewee import IntegerField, Model, TextField

    class File(Model):  # the class name names the index: file_repository_id_path_in_repo
        repository = IntegerField(column_name="repository_id")
        path_in_repo = TextField()

        class Meta:
            table_name = "file"
            indexes = ((("repository", "path_in_repo"), True),)

    File._meta.set_database(database)
    return File


def _unique_pre_a_key(database):
    return [
        index
        for index in database.get_indexes("file")
        if index.unique and list(index.columns) == ["repository_id", "path_in_repo"]
    ]


def test_old_boot_after_033_does_not_rebuild_the_unique_key(drop):
    """D1: the old model's index declaration recreates the pre-A key on every boot
    (peewee's ``CREATE UNIQUE INDEX IF NOT EXISTS``). After 033 that restores
    the refusal of side-branch rows. It must not."""
    module, database, repo = drop
    assert module.run() is True
    assert _unique_pre_a_key(database) == []

    database.create_tables([_pre_a_file_model(database)], safe=True)

    assert _unique_pre_a_key(database) == []


def test_old_writer_after_033_fails_loudly_instead_of_writing(drop):
    """An old replica boots (its declared index is created if missing), then writes: the
    write fails, it does not land on main or on a branch after 033."""
    module, database, repo = drop
    assert module.run() is True
    database.create_tables([_pre_a_file_model(database)], safe=True)  # the old boot
    with pytest.raises(Exception):
        _old_code_upsert(database, repo, "kept.txt", "7" * 64)
    assert database.execute_sql('SELECT COUNT(*) FROM "file"').fetchone()[0] == 1


def test_new_code_boots_and_writes_side_branches_after_033(drop):
    """The new code's own schema creation and side-branch writes keep working after
    033, with the old name occupied by the guard."""
    module, database, repo = drop
    assert module.run() is True
    database.create_tables([File], safe=True)
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="kept.txt",
        size=1,
        sha256="8" * 64,
        owner_id=repo.owner_id,
    )
    assert File.select().where(File.branch == "dev").count() == 1


# --- the operator's rollback command (032 --rollback) -------------------------------


def test_rollback_command_refuses_while_a_non_main_row_exists(expanded, capsys):
    module, database, repo = expanded
    File.create(
        repository=repo,
        branch="dev",
        path_in_repo="dev-only.txt",
        size=1,
        sha256="9" * 64,
        owner_id=repo.owner_id,
    )
    assert module.main(["--rollback"], database, module.cfg) == 1
    assert "1 file row(s) on a non-main branch" in capsys.readouterr().err
    assert "branch" in _columns(database)  # nothing changed


def test_rollback_command_restores_the_pre_a_schema_when_only_main_rows_exist(expanded, capsys):
    module, database, _repo = expanded
    assert module.main(["--rollback"], database, module.cfg) == 0
    assert "rolled back to the pre-A schema" in capsys.readouterr().out
    assert "branch" not in _columns(database)
    assert OLD_INDEX in _index_names(database)


def test_main_without_arguments_runs_the_expand(pre_a):
    module, database, _repo = pre_a
    assert module.main([]) == 0
    assert module.is_applied(database, module.cfg)
