"""Unit tests for quota utilities, on real organization and repository rows.

Usage itself (kohakuhub.usage) is tested against the real services in
test/kohakuhub/test_usage.py.
"""

from __future__ import annotations

import pytest

import kohakuhub.api.quota.util as quota_util
from kohakuhub.db import Repository
from test.kohakuhub.support.factories import make_org, make_repo

pytestmark = pytest.mark.usefixtures("db_scope")


def test_quota_helpers_cover_org_overages_missing_entities_and_ownerless_repositories():
    org = make_org("acme", private_quota_bytes=100, public_quota_bytes=None)
    private_repo = make_repo(org, "private-main", private=True, used_bytes=95)
    make_repo(org, "public-main", private=False, used_bytes=20)

    assert quota_util.check_quota("ghost-org", 1, is_private=True, is_org=True) == (
        False,
        "Organization not found: ghost-org",
    )

    allowed, message = quota_util.check_quota("acme", 10, is_private=True, is_org=True)
    assert allowed is False
    assert "Private storage quota exceeded" in message
    assert quota_util.check_quota("acme", 5, is_private=True, is_org=True) == (
        True,
        None,
    )
    assert quota_util.check_quota("acme", 10**12, is_private=False, is_org=True) == (
        True,
        None,
    )

    info = quota_util.get_storage_info("acme", is_org=True)
    assert info["private_used_bytes"] == 95 and info["private_available_bytes"] == 5
    assert info["public_available_bytes"] is None and info["total_used_bytes"] == 115

    storage_info = quota_util.get_storage_info("ghost")
    assert storage_info["private_quota_bytes"] is None
    assert storage_info["total_used_bytes"] == 0

    # Only the quota column is written: the in-memory usage never reaches the row.
    repo = Repository.get_by_id(private_repo.id)
    repo.used_bytes = 999
    updated_info = quota_util.set_repo_quota(repo, 5)
    stored = Repository.get_by_id(repo.id)
    assert stored.quota_bytes == 5 and stored.used_bytes == 95
    assert updated_info["effective_quota_bytes"] == 5

    with pytest.raises(ValueError, match="exceeds namespace available quota"):
        quota_util.set_repo_quota(repo, 11)

    assert quota_util.get_repo_storage_info(repo)["namespace_available_bytes"] == 5
