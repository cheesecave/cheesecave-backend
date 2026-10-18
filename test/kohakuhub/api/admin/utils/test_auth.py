"""The admin routes' dependency is the shared token check (its behaviour is tested in test/kohakuhub/auth/test_admin_token.py)."""

from __future__ import annotations

import kohakuhub.api.admin.utils as admin_utils
import kohakuhub.api.admin.utils.auth as admin_auth
import kohakuhub.auth.admin_token as admin_token


def test_the_admin_routes_use_the_shared_token_check():
    assert admin_auth.verify_admin_token is admin_token.verify_admin_token
    assert admin_utils.verify_admin_token is admin_token.verify_admin_token
