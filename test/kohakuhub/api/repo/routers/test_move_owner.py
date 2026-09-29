"""A moved repository belongs to the account or organization its new namespace
names, and the migration that repairs repositories moved before (#107).

Everything runs against the real database, LakeFS and bucket.
"""

import importlib.util
from pathlib import Path

import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _file, _live, lfs

MIGRATION = (
    Path(__file__).resolve().parents[5]
    / "scripts"
    / "db_migrations"
    / "022_repository_owner_follows_namespace.py"
)
ORG = "acme-labs"


@pytest.fixture
def m(prepared_backend_test_state):
    ns = type("M", (), {})()
    ns.cfg = _live("kohakuhub.config").cfg
    ns.db = _live("kohakuhub.db")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.ops = _live("kohakuhub.db_operations")
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.quota = _live("kohakuhub.api.quota.util")
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.rest._singleton_client = None
    ns.client = ns.lakefs.get_lakefs_client()
    yield ns
    ns.rest._singleton_client = None


async def _move(client, source, target):
    return await client.post(
        "/api/repos/move", json={"fromRepo": source, "toRepo": target, "type": "model"}
    )


def _row(m, full_id):
    return m.db.Repository.get(m.db.Repository.full_id == full_id)


def _usage(m, name):
    user = m.db.User.get(m.db.User.username == name)
    return user.public_used_bytes + user.private_used_bytes


def _owners(m, row):
    """The owner recorded on the repository, its files and its commits."""
    D = m.db
    return (
        D.Repository.get_by_id(row.id).owner.username,
        {f.owner.username for f in D.File.select().where(D.File.repository == row)},
        {c.owner.username for c in D.Commit.select().where(D.Commit.repository == row)},
    )


async def _filled(m, client, name, organization=None):
    body = {
        "type": "model",
        "name": name,
        **({"organization": organization} if organization else {}),
    }
    response = await client.post("/api/repos/create", json=body)
    assert response.status_code == 200, response.text
    repo = Repo(m, client, name)
    repo.id = f"{organization or 'owner'}/{name}"
    await repo.commit(
        lfs("weights.bin", f"{name} weights".encode() * 40), _file("config.json", "{}")
    )
    await repo.commit(_file("config.json", '{"v": 2}'))
    return repo


def _hand_to(m, row, username):
    """Record ``row`` (and its files and commits) under a new account."""
    D = m.db
    user = m.ops.create_user(username, f"{username}@example.com", "x")
    full_id = f"{username}/{row.name}"
    D.Repository.update(owner=user, namespace=username, full_id=full_id).where(
        D.Repository.id == row.id
    ).execute()
    D.File.update(owner=user).where(D.File.repository == row).execute()
    D.Commit.update(owner=user).where(D.Commit.repository == row).execute()
    return user, full_id


async def test_a_repository_moved_into_an_organization_belongs_to_it(m, owner_client, admin_client):
    repo = await _filled(m, owner_client, "move-hand-over")
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    # A repository of an account that will be deleted afterwards
    leaver, source = _hand_to(m, _row(m, repo.id), "leaver")
    await m.quota.update_namespace_storage("leaver")
    await m.quota.update_namespace_storage(ORG, True)
    size = _row(m, source).used_bytes
    assert size > 0
    user_before, org_before = _usage(m, "leaver"), _usage(m, ORG)

    response = await _move(admin_client, source, f"{ORG}/move-hand-over")
    assert response.status_code == 200, response.text
    row = _row(m, f"{ORG}/move-hand-over")
    assert _owners(m, row) == (ORG, {ORG}, {ORG})
    assert (_usage(m, "leaver"), _usage(m, ORG)) == (user_before - size, org_before + size)
    info = (await owner_client.get(f"/api/quota/repo/model/{ORG}/move-hand-over")).json()
    public = m.db.User.get(m.db.User.username == ORG).public_used_bytes
    assert info["namespace_used_bytes"] == public  # the organization's, not the leaver's

    # Deleting the account it came from leaves it, its commits and its data alone
    lakefs_repo = m.lakefs.resolve_lakefs_repo(row)
    m.ops.delete_user(leaver)
    row = _row(m, f"{ORG}/move-hand-over")
    assert m.db.Commit.select().where(m.db.Commit.repository == row).count() == 2
    branches = {b["id"] for b in (await m.client.list_branches(lakefs_repo))["results"]}
    assert branches == {"main", "dev"}


async def test_a_repository_moved_to_a_user_belongs_to_them(m, owner_client):
    response = await owner_client.post("/org/create", json={"name": "short-lived-org"})
    assert response.status_code == 200, response.text
    repo = await _filled(m, owner_client, "move-to-user", organization="short-lived-org")
    response = await _move(owner_client, repo.id, "owner/move-to-user")
    assert response.status_code == 200, response.text
    row = _row(m, "owner/move-to-user")
    assert _owners(m, row) == ("owner", {"owner"}, {"owner"})
    m.ops.delete_organization(m.db.User.get(m.db.User.username == "short-lived-org"))
    assert m.db.Repository.get_or_none(m.db.Repository.id == row.id) is not None


async def test_an_admin_move_carries_the_usage_along(m, owner_client, admin_client):
    repo = await _filled(m, owner_client, "move-by-admin")
    size = _row(m, repo.id).used_bytes
    user_before, org_before = _usage(m, "owner"), _usage(m, ORG)
    response = await _move(admin_client, repo.id, f"{ORG}/move-by-admin")
    assert response.status_code == 200, response.text
    assert (_usage(m, "owner"), _usage(m, ORG)) == (user_before - size, org_before + size)
    assert _owners(m, _row(m, f"{ORG}/move-by-admin"))[0] == ORG


async def test_a_namespace_nobody_holds_is_refused(m, owner_client, admin_client):
    repo = await _filled(m, owner_client, "move-nowhere")
    response = await _move(admin_client, repo.id, "nobody-holds-this/move-nowhere")
    assert response.status_code == 404
    assert _row(m, repo.id).owner.username == "owner"


async def test_a_rename_keeps_owner_and_usage(m, owner_client):
    repo = await _filled(m, owner_client, "move-rename")
    before = _usage(m, "owner")
    response = await _move(owner_client, repo.id, "owner/move-renamed")
    assert response.status_code == 200, response.text
    assert _owners(m, _row(m, "owner/move-renamed")) == ("owner", {"owner"}, {"owner"})
    assert _usage(m, "owner") == before


def _migration():
    spec = importlib.util.spec_from_file_location("migration_022", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migration_repairs_repositories_moved_before(m, owner_client):
    """Moves before this change kept the owner, and an admin's kept the usage
    where it was: the migration hands such repositories to their namespace."""
    D = m.db
    repo = await _filled(m, owner_client, "moved-long-ago")
    response = await _move(owner_client, repo.id, f"{ORG}/moved-long-ago")
    assert response.status_code == 200, response.text
    row = _row(m, f"{ORG}/moved-long-ago")
    owner = D.User.get(D.User.username == "owner")
    # As the old code left it: the owner kept everywhere, the usage not moved
    D.Repository.update(owner=owner).where(D.Repository.id == row.id).execute()
    D.File.update(owner=owner).where(D.File.repository == row).execute()
    D.Commit.update(owner=owner).where(D.Commit.repository == row).execute()
    D.User.update(public_used_bytes=0, private_used_bytes=0).where(D.User.username == ORG).execute()
    # And a row whose namespace no account holds: reported, left alone
    ghost = D.Repository.create(
        repo_type="model",
        namespace="ghost-namespace",
        name="orphan",
        full_id="ghost-namespace/orphan",
        owner=owner,
        lakefs_repo="m-ghost-namespace-orphan",
    )

    migration = _migration()
    assert not migration.is_applied(D.db, None)
    assert migration.run() is True
    assert _owners(m, row) == (ORG, {ORG}, {ORG})
    R = D.Repository
    summed = sum(r.used_bytes for r in R.select().where(R.namespace == ORG))
    assert _usage(m, ORG) == summed > 0
    assert D.Repository.get_by_id(ghost.id).owner.username == "owner"
    # Applied: running it again changes nothing
    assert migration.is_applied(D.db, None)
    assert migration.run() is True
    assert _owners(m, row) == (ORG, {ORG}, {ORG})


def test_the_migration_reports_a_failure(m, monkeypatch):
    migration = _migration()
    D = m.db
    D.Repository.create(
        repo_type="model",
        namespace="acme-labs",
        name="broken-owner",
        full_id="acme-labs/broken-owner",
        owner=D.User.get(D.User.username == "owner"),
        lakefs_repo="m-broken-owner",
    )

    def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(migration, "_repair", broken)
    assert migration.run() is False


async def test_a_repository_created_in_an_organization_belongs_to_it(m, owner_client):
    """Not to the member who created it: deleting a member must not take an
    organization's repositories along."""
    repo = await _filled(m, owner_client, "made-for-the-org", organization=ORG)
    assert _owners(m, _row(m, repo.id)) == (ORG, {ORG}, {ORG})
    mine = await _filled(m, owner_client, "made-for-me")
    assert _owners(m, _row(m, mine.id)) == ("owner", {"owner"}, {"owner"})
