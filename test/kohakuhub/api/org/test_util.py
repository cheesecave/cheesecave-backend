"""Tests for deprecated organization utility helpers, on real user and membership rows."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import kohakuhub.api.org.util as org_util
from kohakuhub.db import User, UserOrganization
from test.kohakuhub.support.factories import make_org, make_user

pytestmark = pytest.mark.usefixtures("db_scope")

MISSING_ID = 999_999


def _membership(user, org):
    return UserOrganization.get_or_none(
        (UserOrganization.user == user) & (UserOrganization.organization == org)
    )


def test_create_organization_creates_membership_and_rejects_duplicates():
    user = make_user("owner")

    org = org_util.create_organization("acme-labs", "A test org", user)

    assert org.username == "acme-labs" and org.is_org is True
    membership = _membership(user, org)
    assert membership is not None and membership.role == "super-admin"

    with pytest.raises(HTTPException) as exc:
        org_util.create_organization("acme-labs", "A test org", user)
    assert exc.value.status_code == 400
    assert User.select().where(User.username == "acme-labs").count() == 1


def test_get_organization_details_delegates_to_db_operation():
    org = make_org("acme-labs")

    assert org_util.get_organization_details("acme-labs") == org
    assert org_util.get_organization_details("missing-org") is None


def test_add_member_to_organization_covers_validation_and_success():
    org = make_org("acme-labs")
    user = make_user("member")

    org_util.add_member_to_organization(org.id, "member", "admin")
    membership = _membership(user, org)
    assert membership is not None and membership.role == "admin"

    with pytest.raises(HTTPException, match="User not found"):
        org_util.add_member_to_organization(org.id, "missing", "admin")

    with pytest.raises(HTTPException, match="Organization not found"):
        org_util.add_member_to_organization(MISSING_ID, "member", "admin")

    with pytest.raises(HTTPException, match="already a member"):
        org_util.add_member_to_organization(org.id, "member", "admin")


def test_remove_member_from_organization_covers_validation_and_success():
    org = make_org("acme-labs")
    user = make_user("member")
    UserOrganization.create(user=user, organization=org, role="member")

    org_util.remove_member_from_organization(org.id, "member")
    assert _membership(user, org) is None

    with pytest.raises(HTTPException, match="User not found"):
        org_util.remove_member_from_organization(org.id, "missing")

    with pytest.raises(HTTPException, match="Organization not found"):
        org_util.remove_member_from_organization(MISSING_ID, "member")

    with pytest.raises(HTTPException, match="not a member"):
        org_util.remove_member_from_organization(org.id, "member")


def test_get_user_organizations_requires_user_and_returns_memberships():
    org = make_org("acme-labs")
    user = make_user("member")
    UserOrganization.create(user=user, organization=org, role="member")

    memberships = org_util.get_user_organizations(user.id)
    assert [membership.organization_id for membership in memberships] == [org.id]

    with pytest.raises(HTTPException, match="User not found"):
        org_util.get_user_organizations(MISSING_ID)


def test_update_member_role_covers_validation_and_success():
    org = make_org("acme-labs")
    user = make_user("member")
    UserOrganization.create(user=user, organization=org, role="member")

    org_util.update_member_role(org.id, "member", "admin")
    assert _membership(user, org).role == "admin"

    with pytest.raises(HTTPException, match="User not found"):
        org_util.update_member_role(org.id, "missing", "admin")

    with pytest.raises(HTTPException, match="Organization not found"):
        org_util.update_member_role(MISSING_ID, "member", "admin")

    _membership(user, org).delete_instance()
    with pytest.raises(HTTPException, match="not a member"):
        org_util.update_member_role(org.id, "member", "admin")
