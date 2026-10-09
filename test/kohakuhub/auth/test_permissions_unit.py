"""Unit tests for permission branches not covered by integration tests, on real user, organization and repository rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.auth.permissions as permissions
from kohakuhub.db import Repository
from test.kohakuhub.support.factories import make_org, make_repo, make_user

pytestmark = pytest.mark.usefixtures("db_scope")


def test_namespace_permission_covers_admin_bypass_and_common_failures():
    user = make_user("owner")

    assert permissions.check_namespace_permission("any", None, is_admin=True) is True

    with pytest.raises(HTTPException) as no_user_exc:
        permissions.check_namespace_permission("acme", None)
    assert no_user_exc.value.status_code == 403

    with pytest.raises(HTTPException) as missing_org_exc:
        permissions.check_namespace_permission("missing-org", user)
    assert missing_org_exc.value.status_code == 403

    make_org("acme")
    with pytest.raises(HTTPException) as membership_exc:
        permissions.check_namespace_permission("acme", user)
    assert membership_exc.value.status_code == 403


def test_repo_read_permission_covers_admin_public_owner_and_unauthenticated_paths():
    owner = make_user("owner")
    public_repo = make_repo(owner, "public")
    private_repo = make_repo(owner, "private", private=True)

    assert permissions.check_repo_read_permission(private_repo, None, is_admin=True) is True
    assert permissions.check_repo_read_permission(public_repo, None) is True
    assert permissions.check_repo_read_permission(private_repo, owner) is True

    # The read query decides: a signed-in user with no membership cannot read the private repo.
    with pytest.raises(permissions.RepoReadDeniedError):
        permissions.check_repo_read_permission(private_repo, make_user("outsider"))

    # Anonymous-on-private now collapses to RepoReadDeniedError (privacy-
    # preserving Option A from #76 — same wire shape as authed-no-access,
    # both translate to ``404 + X-Error-Code: RepoNotFound`` in main.py's
    # global handler).
    with pytest.raises(permissions.RepoReadDeniedError) as unauth_exc:
        permissions.check_repo_read_permission(private_repo, None)
    assert unauth_exc.value.repo_id == "owner/private"
    assert unauth_exc.value.repo_type == "model"


def test_repo_write_and_delete_permission_cover_admin_owner_and_org_admin():
    owner = make_user("owner")
    acme = make_org("acme", admin=owner)
    repo = make_repo(acme, "repo", private=True)
    owner_repo = make_repo(owner, "repo", private=True)

    assert permissions.check_repo_write_permission(repo, None, is_admin=True) is True
    assert permissions.check_repo_write_permission(owner_repo, owner) is True
    assert permissions.check_repo_delete_permission(repo, None, is_admin=True) is True
    assert permissions.check_repo_delete_permission(owner_repo, owner) is True

    with pytest.raises(HTTPException) as write_no_user_exc:
        permissions.check_repo_write_permission(repo, None)
    assert write_no_user_exc.value.status_code == 403

    with pytest.raises(HTTPException) as delete_no_user_exc:
        permissions.check_repo_delete_permission(repo, None)
    assert delete_no_user_exc.value.status_code == 403

    # Admin membership of the organization (a real UserOrganization row) allows delete.
    assert permissions.check_repo_delete_permission(repo, owner) is True


def test_filter_readable_repositories_narrows_to_the_requested_author_namespace():
    owner = make_user("owner")
    other = make_user("other")
    make_repo(owner, "mine")
    make_repo(other, "theirs")
    make_repo(other, "hidden", private=True)

    # Anonymous readers see only public rows, and ``author`` keeps only that namespace.
    by_owner = permissions.filter_readable_repositories(Repository.select(), None, author="owner")
    assert {repo.full_id for repo in by_owner} == {"owner/mine"}

    by_other = permissions.filter_readable_repositories(Repository.select(), None, author="other")
    assert {repo.full_id for repo in by_other} == {"other/theirs"}

    unfiltered = permissions.filter_readable_repositories(Repository.select(), None)
    assert {repo.full_id for repo in unfiltered} == {"owner/mine", "other/theirs"}
