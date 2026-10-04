"""Each path's last commit on main is recorded as commits land, and a file
listing reads it instead of asking LakeFS (kohakuhub.path_commits)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _live
from test.kohakuhub.api.helpers import encode_ndjson

MIGRATION = (
    Path(__file__).resolve().parents[3] / "scripts" / "db_migrations" / "026_path_commits.py"
)


@pytest.fixture
def m(prepared_backend_test_state, monkeypatch):
    _live("kohakuhub.lakefs_rest_client")._singleton_client = None
    cfg = _live("kohakuhub.config").cfg
    for flag in ("repository_revert_enabled", "repository_reset_enabled", "repository_squash_enabled"):
        monkeypatch.setattr(cfg.app, flag, True)
    ns = type("M", (), {})()
    ns.cfg = cfg
    ns.db = _live("kohakuhub.db")
    ns.pc = _live("kohakuhub.path_commits")
    ns.tasks = _live("kohakuhub.tasks")
    ns.tree = _live("kohakuhub.api.repo.routers.tree")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    yield ns
    _live("kohakuhub.lakefs_rest_client")._singleton_client = None


async def _repo(m, client, name):
    return await Repo(m, client, name).create()


def _rows(m, repo):
    P = m.db.PathCommit
    row = m.db.Repository.get(m.db.Repository.full_id == repo.id)
    return {
        p.path: p.title
        for p in P.select().where((P.repository == row) & (P.branch == "main"))
    }


async def _expanded(repo, paths, revision="main"):
    response = await repo.client.post(
        f"/api/models/{repo.id}/paths-info/{revision}",
        data={"paths": paths, "expand": "true"},
    )
    assert response.status_code == 200, response.text
    return {e["path"]: e.get("lastCommit") for e in response.json()}


@pytest.fixture
def no_lakefs_lookups(m, monkeypatch):
    """Fail if a listing asks LakeFS for a last commit."""
    asked = []
    original = m.tree.resolve_last_commits_for_paths

    async def resolve(lakefs_repo, revision, targets):
        asked.extend(t["path"] for t in targets)
        return await original(lakefs_repo, revision, targets)

    monkeypatch.setattr(m.tree, "resolve_last_commits_for_paths", resolve)
    return asked


async def test_a_commit_records_its_files_and_their_folders(m, owner_client, no_lakefs_lookups):
    repo = await _repo(m, owner_client, "pc-record")
    first = await repo.commit(_file("a/b/c.txt", "1"), _file("d.txt", "1"), summary="first")
    second = await repo.commit(_file("a/b/c.txt", "2"), summary="second")

    assert _rows(m, repo) == {"a": "second", "a/b": "second", "a/b/c.txt": "second", "d.txt": "first"}
    expanded = await _expanded(repo, ["a", "a/b", "a/b/c.txt", "d.txt"])
    assert {p: c["id"] for p, c in expanded.items()} == {
        "a": second, "a/b": second, "a/b/c.txt": second, "d.txt": first,
    }
    assert expanded["a"]["title"] == "second" and expanded["a"]["date"]
    assert no_lakefs_lookups == []  # all recorded: LakeFS is not asked


async def test_the_tree_listing_reads_the_records_too(m, owner_client, no_lakefs_lookups):
    repo = await _repo(m, owner_client, "pc-tree")
    commit = await repo.commit(_file("docs/x.md", "x"), _file("top.txt", "t"), summary="only")

    response = await owner_client.get(f"/api/models/{repo.id}/tree/main", params={"expand": "true"})

    assert response.status_code == 200
    got = {e["path"]: (e.get("lastCommit") or {}).get("id") for e in response.json()}
    assert got["docs"] == commit and got["top.txt"] == commit
    assert no_lakefs_lookups == []


async def test_deleting_files_moves_their_folders(m, owner_client):
    repo = await _repo(m, owner_client, "pc-delete")
    await repo.commit(_file("a/x.txt", "x"), _file("a/y.txt", "y"), _file("b/z.txt", "z"), summary="add")
    await repo.commit(_delete("a/y.txt"), summary="drop y")
    response = await owner_client.post(
        f"/api/models/{repo.id}/commit/main",
        content=encode_ndjson(
            [
                {"key": "header", "value": {"summary": "drop b"}},
                {"key": "deletedFolder", "value": {"path": "b"}},
            ]
        ),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert response.status_code == 200, response.text

    rows = _rows(m, repo)
    assert rows["a"] == "drop y" and rows["a/x.txt"] == "add"
    assert rows["b"] == "drop b"


async def test_only_main_is_recorded(m, owner_client):
    repo = await _repo(m, owner_client, "pc-branch")
    await repo.commit(_file("a/f.txt", "main"), summary="on main")
    branch = await owner_client.post(f"/api/models/{repo.id}/branch", json={"branch": "dev"})
    assert branch.status_code == 200, branch.text
    dev = await repo.commit(_file("a/f.txt", "dev"), branch="dev", summary="on dev")

    assert _rows(m, repo) == {"a": "on main", "a/f.txt": "on main"}
    # Another revision is looked up the old way: files only
    expanded = await _expanded(repo, ["a", "a/f.txt"], revision="dev")
    assert expanded == {"a": None, "a/f.txt": expanded["a/f.txt"]}
    assert expanded["a/f.txt"]["id"] == dev


async def test_a_path_without_a_record_falls_back(m, owner_client):
    repo = await _repo(m, owner_client, "pc-fallback")
    commit = await repo.commit(_file("dir/f.txt", "f"), summary="one")
    P = m.db.PathCommit
    P.delete().where(P.title == "one").execute()

    expanded = await _expanded(repo, ["dir", "dir/f.txt"])

    assert expanded["dir"] is None  # a directory is not looked up in LakeFS
    assert expanded["dir/f.txt"]["id"] == commit


async def test_revert_and_reset_record_their_commits(m, owner_client):
    repo = await _repo(m, owner_client, "pc-history")
    base = await repo.commit(_file("k/f.txt", "1"), _file("g.txt", "g"), summary="base")
    changed = await repo.commit(_file("k/f.txt", "2"), summary="changed")

    reverted = await owner_client.post(
        f"/api/models/{repo.id}/branch/main/revert", json={"ref": changed}
    )
    assert reverted.status_code == 200, reverted.text
    rows = _rows(m, repo)
    assert rows["k/f.txt"] == rows["k"] != "changed" and rows["g.txt"] == "base"
    revert_title = rows["k"]

    await repo.commit(_file("g.txt", "g2"), summary="g again")
    reset = await owner_client.post(
        f"/api/models/{repo.id}/branch/main/reset", json={"ref": base, "force": True}
    )
    assert reset.status_code == 200, reset.text
    rows = _rows(m, repo)
    assert rows["g.txt"] not in ("base", "g again")  # the reset commit
    assert rows["k/f.txt"] == revert_title  # unchanged by the reset: same content


async def test_a_squash_makes_its_commit_everyones_last(m, owner_client):
    repo = await _repo(m, owner_client, "pc-squash")
    await repo.commit(_file("a/f.txt", "1"), summary="one")
    await repo.commit(_file("g.txt", "2"), summary="two")

    response = await owner_client.post(
        "/api/repos/squash", json={"repo": repo.id, "type": "model", "message": "squashed"}
    )
    assert response.status_code == 200, response.text

    head = await repo.head()
    expanded = await _expanded(repo, ["a", "a/f.txt", "g.txt"])
    assert {c["id"] for c in expanded.values()} == {head}
    assert set(_rows(m, repo).values()) == {"squashed"}


async def test_the_backfill_records_an_existing_repository(m, owner_client):
    repo = await _repo(m, owner_client, "pc-backfill")
    await repo.commit(_file("a/b/c.txt", "1"), _file("d.txt", "1"), summary="first")
    await repo.commit(_file("a/b/c.txt", "2"), summary="second")
    await repo.commit(_delete("d.txt"), _file("e.txt", "e"), summary="third")
    recorded = _rows(m, repo)
    R, P = m.db.Repository, m.db.PathCommit
    row = R.get(R.full_id == repo.id)
    P.delete().where(P.repository == row).execute()
    R.update(last_commits_recorded=False).where(R.id == row.id).execute()
    # A commit that lands while the backfill runs is newer: never replaced
    P.create(repository=row, branch="main", path="e.txt", commit_id="f" * 64, title="newer", date=2**40)

    assert m.pc.ensure_backfill() is not None
    assert m.pc.ensure_backfill() is None  # pending already

    await m.pc.backfill({}, _Context())

    got = _rows(m, repo)
    assert got.pop("e.txt") == "newer"
    recorded.pop("e.txt")
    assert got == recorded  # d.txt, deleted, keeps the deleting commit: harmless
    assert R.get_by_id(row.id).last_commits_recorded is True
    assert not R.select().where(R.last_commits_recorded == False).exists()  # noqa: E712
    T = m.db.BackgroundTask
    T.delete().where(T.kind == m.pc.BACKFILL_KIND).execute()
    assert m.pc.ensure_backfill() is None  # nothing left to record


async def test_the_backfill_records_a_squashed_repository(m, owner_client):
    repo = await _repo(m, owner_client, "pc-backfill-squash")
    await repo.commit(_file("a/f.txt", "1"), summary="one")
    response = await owner_client.post(
        "/api/repos/squash", json={"repo": repo.id, "type": "model", "message": "squashed"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(_file("g.txt", "2"), summary="after")
    R, P = m.db.Repository, m.db.PathCommit
    row = R.get(R.full_id == repo.id)
    P.delete().where(P.repository == row).execute()
    R.update(last_commits_recorded=False).where(R.id == row.id).execute()

    await m.pc.backfill({}, _Context())

    assert _rows(m, repo) == {"a": "squashed", "a/f.txt": "squashed", "g.txt": "after"}


class _Context:
    def __init__(self):
        self.stages, self.done = [], []

    def stage(self, name):
        self.stages.append(name)

    def progress(self, done, total=None):
        self.done.append((done, total))


def test_new_repositories_record_from_the_start(m):
    R = m.db.Repository
    assert R.last_commits_recorded.default is True


def _migration():
    spec = importlib.util.spec_from_file_location("migration_026", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_adds_the_records(m):
    migration = _migration()
    D = m.db
    assert migration.is_applied(D.db, m.cfg)
    with D.db.atomic() as transaction:
        D.db.execute_sql('ALTER TABLE "repository" DROP COLUMN "last_commits_recorded"')
        D.db.execute_sql('DROP TABLE "path_commit"')
        assert not migration.is_applied(D.db, m.cfg)
        assert migration.run() is True
        assert migration.is_applied(D.db, m.cfg)
        # Existing repositories are to be recorded by the backfill
        assert {r.last_commits_recorded for r in D.Repository.select()} == {False}
        assert migration.run() is True  # applied: nothing again
        transaction.rollback()
    with D.db.atomic() as transaction:  # the table made by init_db, the column missing
        D.db.execute_sql('ALTER TABLE "repository" DROP COLUMN "last_commits_recorded"')
        assert not migration.is_applied(D.db, m.cfg)
        assert migration.run() is True
        assert migration.is_applied(D.db, m.cfg)
        transaction.rollback()
    with D.db.atomic() as transaction:  # the column there, the table missing
        D.db.execute_sql('DROP TABLE "path_commit"')
        assert not migration.is_applied(D.db, m.cfg)
        assert migration.run() is True
        assert migration.is_applied(D.db, m.cfg)
        transaction.rollback()
    for before in ('DROP TABLE "site_branding"', 'ALTER TABLE "repository" DROP COLUMN "history_root"'):
        with D.db.atomic() as transaction:  # never applied on a schema missing what came before
            D.db.execute_sql(before)
            assert not migration.is_applied(D.db, m.cfg)
            transaction.rollback()


def test_the_migration_skips_when_a_later_one_is_applied(m, monkeypatch):
    migration = _migration()
    monkeypatch.setattr(migration, "should_skip_due_to_future_migrations", lambda *args: True)
    monkeypatch.setattr(migration, "_migrate", lambda *args: (_ for _ in ()).throw(AssertionError))
    assert migration.run() is True


def test_the_migration_reports_a_failure(m, monkeypatch):
    migration = _migration()
    monkeypatch.setattr(migration, "is_applied", lambda db, cfg: False)
    monkeypatch.setattr(migration, "_migrate", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert migration.run() is False


async def test_the_backfill_pages_through_lakefs(m, owner_client, monkeypatch):
    """One entry per LakeFS page: the log, the diffs and the listing of a
    parentless commit are all read to the end."""
    repo = await _repo(m, owner_client, "pc-pages")
    await repo.commit(_file("a/x.txt", "1"), _file("a/y.txt", "1"), summary="one")
    await repo.commit(_file("a/x.txt", "2"), _file("b.txt", "2"), summary="two")
    recorded = _rows(m, repo)
    R, P = m.db.Repository, m.db.PathCommit
    row = R.get(R.full_id == repo.id)
    P.delete().where(P.repository == row).execute()
    R.update(last_commits_recorded=False).where(R.id == row.id).execute()
    monkeypatch.setattr(m.pc, "LAKEFS_PAGE", 1)

    context = _Context()
    await m.pc.backfill({}, context)

    assert _rows(m, repo) == recorded
    assert context.done[-1][0] == context.done[-1][1]


async def test_the_backfill_skips_a_repository_lakefs_lost_and_retries_on_errors(
    m, owner_client, monkeypatch
):
    repo = await _repo(m, owner_client, "pc-lost")
    R = m.db.Repository
    row = R.get(R.full_id == repo.id)
    R.update(last_commits_recorded=False).where(R.id == row.id).execute()
    calls = []
    backfill_repository = m.pc.backfill_repository

    async def lost(client, repository):
        calls.append(repository.full_id)
        raise RuntimeError("LakeFS is unreachable")

    monkeypatch.setattr(m.pc, "backfill_repository", lost)
    with pytest.raises(RuntimeError):  # the task fails, and is retried
        await m.pc.backfill({}, _Context())
    assert R.get_by_id(row.id).last_commits_recorded is False

    monkeypatch.setattr(m.pc, "backfill_repository", backfill_repository)
    monkeypatch.setattr(m.pc, "resolve_lakefs_repo", lambda repository: "no-such-lakefs-repository")
    await m.pc.backfill({}, _Context())  # gone from LakeFS: nothing to record
    assert R.get_by_id(row.id).last_commits_recorded is True
    assert calls == [repo.id]


async def test_failing_to_record_never_fails_the_change(m, owner_client, monkeypatch):
    repo = await _repo(m, owner_client, "pc-resilient")

    async def broken(*args):
        raise RuntimeError("database is gone")

    def broken_squash(*args):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(m.pc, "record", broken)
    monkeypatch.setattr(m.pc, "record_squash", broken_squash)
    await repo.commit(_file("f.txt", "1"), summary="still committed")
    again = await repo.commit(_file("f.txt", "2"), summary="again")
    reverted = await owner_client.post(f"/api/models/{repo.id}/branch/main/revert", json={"ref": again})
    response = await owner_client.post("/api/repos/squash", json={"repo": repo.id, "type": "model"})

    assert reverted.status_code == 200, reverted.text
    assert response.status_code == 200, response.text
    assert _rows(m, repo) == {}  # a listing then asks LakeFS, bounded


def test_folders_of_odd_paths():
    from kohakuhub.path_commits import with_folders

    assert with_folders(["a//b/", "/c", ""]) == {"a", "a/b", "c"}
