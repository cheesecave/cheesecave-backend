"""Admin authentication utilities.

The check itself lives in ``kohakuhub.auth.admin_token`` so that the routes which accept either a user or the admin token
go through the very same function (throttle, weak-token guard, client rules).
"""

from kohakuhub.auth.admin_token import verify_admin_token

__all__ = ["verify_admin_token"]
