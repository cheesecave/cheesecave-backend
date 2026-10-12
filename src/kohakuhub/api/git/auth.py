"""Who a git or git-lfs request is from.

``git`` and ``git-lfs`` send HTTP Basic (the user name is ignored, the token is
the password); ``huggingface_hub`` sends Bearer; the web UI sends a session
cookie. One resolver serves the git routes and the LFS routes, so a credential
that works for ``git clone`` also works for the LFS download that follows. A
credential that does not check out means "anonymous", never an error: what an
anonymous caller may do is decided by the route.
"""

from typing import Optional

from fastapi import Cookie, Header, Request

from kohakuhub.api.git.utils.server import parse_git_credentials
from kohakuhub.auth.dependencies import get_optional_user, get_user_by_token
from kohakuhub.db import User


def get_git_user(
    request: Request,
    session_id: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Optional[User]:
    """The caller of a git or LFS route, or None for an anonymous one."""
    if authorization and authorization.startswith("Basic "):
        _, token = parse_git_credentials(authorization)
        user = get_user_by_token(token) if token else None
        if user:
            return user
        # a wrong password is anonymous, but a session cookie still identifies the web UI
        authorization = None
    return get_optional_user(request, session_id, authorization)
