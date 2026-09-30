"""Unit tests for quota utilities.

Usage itself (kohakuhub.usage) is tested against the real services in
test/kohakuhub/test_usage.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import kohakuhub.api.quota.util as quota_util


class _MutableRepo(SimpleNamespace):
    def save(self, only=None):
        self.saved = only


class _FakeUserModel:
    get_or_none_result = None
    username = "username"

    @classmethod
    def get_or_none(cls, *args, **kwargs):
        return cls.get_or_none_result


def test_quota_helpers_cover_org_overages_missing_entities_and_ownerless_repositories(
    monkeypatch,
):
    used = {"acme": {"private": 95, "public": 20}}
    monkeypatch.setattr(
        quota_util,
        "namespace_usage",
        lambda names: {
            name: used.get(name, {"private": 0, "public": 0}) for name in names
        },
    )
    monkeypatch.setattr(
        quota_util,
        "namespace_used",
        lambda name, private: used.get(name, {"private": 0, "public": 0})[
            "private" if private else "public"
        ],
    )
    monkeypatch.setattr(quota_util, "User", _FakeUserModel)
    monkeypatch.setattr(quota_util, "get_organization", lambda namespace: None)
    assert quota_util.check_quota("acme", 1, is_private=True, is_org=True) == (
        False,
        "Organization not found: acme",
    )

    org = SimpleNamespace(private_quota_bytes=100, public_quota_bytes=None)
    monkeypatch.setattr(quota_util, "get_organization", lambda namespace: org)
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

    _FakeUserModel.get_or_none_result = None
    storage_info = quota_util.get_storage_info("ghost")
    assert storage_info["private_quota_bytes"] is None
    assert storage_info["total_used_bytes"] == 0

    orphan_repo = _MutableRepo(
        owner=None,
        namespace="ghost",
        private=False,
        quota_bytes=None,
        used_bytes=3,
        full_id="ghost/orphan",
    )
    repo_info = quota_util.get_repo_storage_info(orphan_repo)
    assert repo_info["namespace_quota_bytes"] is None
    assert repo_info["namespace_used_bytes"] == 0

    updated_info = quota_util.set_repo_quota(orphan_repo, 10)
    assert orphan_repo.quota_bytes == 10
    assert orphan_repo.saved == [
        quota_util.Repository.quota_bytes
    ]  # nothing else is written
    assert updated_info["effective_quota_bytes"] == 10

    org_repo = _MutableRepo(
        owner=org,
        namespace="acme",
        private=True,
        quota_bytes=None,
        used_bytes=50,
        full_id="acme/r",
    )
    assert quota_util.get_repo_storage_info(org_repo)["namespace_available_bytes"] == 5
