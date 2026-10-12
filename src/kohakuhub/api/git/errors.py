"""How the git and git-lfs routes say no.

Real clients decide what to do from the status line and the challenge header:
``git`` retries with the credentials in the URL only after a ``401`` that carries
``WWW-Authenticate``, and ``git-lfs`` does the same with ``LFS-Authenticate``.
So these routes answer in the clients' own shapes (plain text for git, the
``application/vnd.git-lfs+json`` body for LFS) instead of the API's JSON.
"""

from fastapi.responses import JSONResponse, PlainTextResponse, Response

GIT = "git"
LFS = "lfs"

GIT_CHALLENGE = 'Basic realm="git-frontend"'
LFS_CHALLENGES = (
    'Basic realm="Authentication required", charset="UTF-8"',
    'Bearer realm="Authentication required"',
)
LFS_CONTENT_TYPE = "application/vnd.git-lfs+json"

_MESSAGES = {
    (GIT, 401): "Invalid username or password.",
    (LFS, 401): "Authentication required",
}
_DEFAULT_MESSAGES = {
    404: "Repository not found",
    403: "You do not have permission to write to this repository",
}


class GitProtocolError(Exception):
    """An error to answer in the shape the named protocol's client expects."""

    def __init__(self, status_code: int, protocol: str, message: str | None = None):
        self.status_code = status_code
        self.protocol = protocol
        self.message = (
            message
            or _MESSAGES.get((protocol, status_code))
            or _DEFAULT_MESSAGES.get(status_code, "Error")
        )
        super().__init__(self.message)


def unauthorized(protocol: str) -> GitProtocolError:
    return GitProtocolError(401, protocol)


def not_found(protocol: str) -> GitProtocolError:
    return GitProtocolError(404, protocol)


def forbidden(protocol: str) -> GitProtocolError:
    return GitProtocolError(403, protocol)


def render_protocol_error(exc: GitProtocolError) -> Response:
    """The response for ``exc``, with the challenge a 401 needs."""
    if exc.protocol == LFS:
        headers = {"X-Error-Message": exc.message}
        if exc.status_code == 404:
            # what huggingface_hub keys on to raise RepositoryNotFoundError
            headers["X-Error-Code"] = "RepoNotFound"
        response = JSONResponse(
            {"message": exc.message, "error": exc.message},
            status_code=exc.status_code,
            media_type=LFS_CONTENT_TYPE,
            headers=headers,
        )
        if exc.status_code == 401:
            # two headers, one per scheme, as the Hub sends them (a dict cannot repeat a key)
            for challenge in LFS_CHALLENGES:
                response.raw_headers.append((b"lfs-authenticate", challenge.encode()))
        return response
    headers = {"WWW-Authenticate": GIT_CHALLENGE} if exc.status_code == 401 else None
    return PlainTextResponse(exc.message, status_code=exc.status_code, headers=headers)
