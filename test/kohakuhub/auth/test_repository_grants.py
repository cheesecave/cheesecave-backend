"""Single-repository grants share the live SQL read policy without write escalation."""

from datetime import timedelta

import pytest
from fastapi import HTTPException

from kohakuhub.auth import permissions
from kohakuhub.db import Repository, RepositoryAccessGrant, UserOrganization, utcnow
from test.kohakuhub.support.factories import make_org, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


def test_grant_is_repository_scoped_read_only_and_shared_by_direct_and_sql_reads():
    owner = make_user("owner")
    reader = make_user("reader")
    org = make_org("organization")
    public = make_repo(owner, "public")
    granted = make_repo(org, "granted", private=True)
    sibling = make_repo(org, "sibling", private=True)
    RepositoryAccessGrant.create(repository=granted, user=reader, granted_by=owner)

    assert permissions.check_repo_read_permission(granted, reader)
    assert {r.id for r in permissions.filter_readable_repositories(Repository.select(), reader)} == {
        public.id, granted.id
    }
    assert not UserOrganization.select().where(UserOrganization.user == reader).exists()
    for user in (None, owner):
        with pytest.raises(permissions.RepoReadDeniedError):
            permissions.check_repo_read_permission(granted, user)
    with pytest.raises(permissions.RepoReadDeniedError):
        permissions.check_repo_read_permission(sibling, reader)
    for check in (
        permissions.check_repo_write_permission,
        permissions.check_repo_delete_permission,
        permissions.check_repo_manage_permission,
    ):
        with pytest.raises(HTTPException) as denied:
            check(granted, reader)
        assert denied.value.status_code == 403


def test_expired_revoked_and_other_user_grants_are_denied_and_revocation_is_live():
    owner, reader, other = (make_user(name) for name in ("owner", "reader", "other"))
    repo = make_repo(owner, "private", private=True)
    grant = RepositoryAccessGrant.create(
        repository=repo, user=reader, expires_at=utcnow() + timedelta(days=1)
    )
    query = permissions.filter_readable_repositories(Repository.select(), reader)
    assert [r.id for r in query] == [repo.id]
    grant.revoked_at = utcnow()
    grant.save()
    assert list(query.clone()) == []
    with pytest.raises(permissions.RepoReadDeniedError):
        permissions.check_repo_read_permission(repo, reader)
    grant.revoked_at = None
    grant.expires_at = utcnow() - timedelta(seconds=1)
    grant.save()
    with pytest.raises(permissions.RepoReadDeniedError):
        permissions.check_repo_read_permission(repo, reader)
    grant.expires_at = None
    grant.save()
    assert permissions.check_repo_read_permission(repo, reader)
    with pytest.raises(permissions.RepoReadDeniedError):
        permissions.check_repo_read_permission(repo, other)


def test_compiled_alias_predicate_matches_granted_repository_sql(db_scope):
    owner, reader = make_user("owner"), make_user("reader")
    public = make_repo(owner, "public")
    private = make_repo(owner, "private", private=True)
    make_repo(owner, "hidden", private=True)
    RepositoryAccessGrant.create(repository=private, user=reader)
    sql, params = permissions.compile_repository_predicate(
        permissions.repository_read_predicate(reader, Repository.alias("r")), db_scope
    )
    actual = {
        row[0] for row in db_scope.execute_sql("SELECT r.id FROM repository r WHERE " + sql, params)
    }
    assert actual == {public.id, private.id}
    assert actual == {r.id for r in permissions.filter_readable_repositories(Repository.select(), reader)}


def test_management_uses_owner_foreign_key_not_a_matching_namespace():
    owner, other = make_user("owner"), make_user("other")
    repo = make_repo(owner, "repo", private=True, namespace=other.username)
    assert permissions.check_repo_manage_permission(repo, owner)
    assert permissions.check_repo_manage_permission(repo, None, is_admin=True)
    for user in (None, other):
        with pytest.raises(HTTPException) as denied:
            permissions.check_repo_manage_permission(repo, user)
        assert denied.value.status_code == 403


def test_membership_row_pointing_at_a_person_does_not_grant_management():
    owner, reader = make_user("owner"), make_user("reader")
    repo = make_repo(owner, "repo", private=True)
    UserOrganization.create(user=reader, organization=owner, role="admin")
    with pytest.raises(HTTPException):
        permissions.check_repo_manage_permission(repo, reader)


@pytest.mark.parametrize("role,allowed", [("visitor", False), ("member", False), ("admin", True), ("super-admin", True)])
def test_management_requires_current_admin_membership_of_actual_owner(role, allowed):
    reader = make_user("reader")
    owner = make_org("owner")
    unrelated = make_org("unrelated", admin=reader)
    repo = make_repo(owner, "repo", private=True, namespace=unrelated.username)
    membership = UserOrganization.create(user=reader, organization=owner, role=role)
    if allowed:
        assert permissions.check_repo_manage_permission(repo, reader)
    else:
        with pytest.raises(HTTPException):
            permissions.check_repo_manage_permission(repo, reader)
    membership.delete_instance()
    with pytest.raises(HTTPException):
        permissions.check_repo_manage_permission(repo, reader)
