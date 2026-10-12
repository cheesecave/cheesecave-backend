"""Git Smart HTTP Protocol endpoints.

This module implements the read side of Git Smart HTTP (clone, fetch, pull) for
models, datasets and Spaces. A push is refused (501) until it is implemented.

Every route answers in the shape git itself understands (see
``kohakuhub.api.git.errors``): ``git`` only sends the credentials in the URL
after a ``401`` that carries a challenge, so the answer to its first,
credential-less request decides whether authentication can work at all.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from kohakuhub.api.git.auth import get_git_user
from kohakuhub.api.git.errors import (
    GIT,
    GitProtocolError,
    forbidden,
    not_found,
    unauthorized,
)
from kohakuhub.api.git.utils.lakefs_bridge import GitLakeFSBridge
from kohakuhub.api.git.utils.server import (
    GitReceivePackHandler,
    GitUploadPackHandler,
)
from kohakuhub.auth.permissions import (
    RepoReadDeniedError,
    check_repo_read_permission,
    check_repo_write_permission,
)
from kohakuhub.db import Repository, User
from kohakuhub.db_operations import get_repository
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import resolve_lakefs_repo

logger = get_logger("GIT_HTTP")
router = APIRouter()

REPO_TYPES = ("model", "dataset", "space")
UPLOAD_PACK = "git-upload-pack"
RECEIVE_PACK = "git-receive-pack"

# A repository is addressed with or without its type prefix and with or without
# ``.git``. Without a prefix the type is not known, so the first match wins
# (model, then dataset, then Space); a prefix names the type exactly.
URL_FORMS = (
    "/{repo_type}s/{namespace}/{name}.git",
    "/{namespace}/{name}.git",
    "/{repo_type}s/{namespace}/{name}",
    "/{namespace}/{name}",
)


def _routes(method: str, suffix: str):
    """Register a handler under every URL form of a repository."""

    def decorator(handler):
        for form in URL_FORMS:
            handler = getattr(router, method)(form + suffix)(handler)
        return handler

    return decorator


def _find_repository(
    repo_type: str | None, namespace: str, name: str
) -> Repository | None:
    if repo_type is not None:
        if repo_type not in REPO_TYPES:
            raise HTTPException(404, detail="Not found")
        return get_repository(repo_type, namespace, name)
    for candidate in REPO_TYPES:
        repo = get_repository(candidate, namespace, name)
        if repo:
            return repo
    return None


def _can_read(repo: Repository, user: User | None) -> bool:
    try:
        check_repo_read_permission(repo, user)
    except RepoReadDeniedError:
        return False
    return True


def _can_write(repo: Repository, user: User) -> bool:
    try:
        check_repo_write_permission(repo, user)
    except HTTPException:
        return False
    return True


def _authorize_read(repo: Repository | None, user: User | None) -> Repository:
    """A repository the caller may not see answers like one that does not exist.

    An unauthenticated caller is asked to authenticate (so git sends its
    credentials); a signed-in one is told it was not found.
    """
    if repo is None or not _can_read(repo, user):
        raise unauthorized(GIT) if user is None else not_found(GIT)
    return repo


def _authorize_write(repo: Repository | None, user: User | None) -> Repository:
    """A push needs a valid user before the repository is even looked at, and a
    repository the caller cannot see is not found; 403 only where it is visible."""
    if user is None:
        raise unauthorized(GIT)
    if repo is None or not _can_read(repo, user):
        raise not_found(GIT)
    if not _can_write(repo, user):
        raise forbidden(GIT)
    return repo


def _check_service(service: str | None) -> str:
    if service not in (UPLOAD_PACK, RECEIVE_PACK):
        raise GitProtocolError(400, GIT, "invalid service")
    return service


@_routes("get", "/info/refs")
async def git_info_refs(
    namespace: str,
    name: str,
    repo_type: str | None = None,
    service: str | None = Query(None),
    user: User | None = Depends(get_git_user),
):
    """Git info/refs endpoint for service advertisement.

    Advertises the refs and capabilities for ``git-upload-pack`` (clone, fetch,
    pull) or ``git-receive-pack`` (push).
    """
    service = _check_service(service)
    repo_id = f"{namespace}/{name}"
    logger.info(f"Git info/refs: {service} for {repo_id}")

    repo = _find_repository(repo_type, namespace, name)
    if service == UPLOAD_PACK:
        repo = _authorize_read(repo, user)
    else:
        repo = _authorize_write(repo, user)

    # Get refs from LakeFS using the repository's actual type
    bridge = GitLakeFSBridge(
        repo.repo_type, namespace, name, lakefs_repo=resolve_lakefs_repo(repo)
    )
    refs = await bridge.get_refs(branch="main")

    if service == UPLOAD_PACK:
        response_data = GitUploadPackHandler(repo_id).get_service_info(refs)
    else:
        response_data = GitReceivePackHandler(repo_id).get_service_info(refs)

    return Response(
        content=response_data,
        media_type=f"application/x-{service}-advertisement",
        headers={"Cache-Control": "no-cache"},
    )


@_routes("post", "/git-upload-pack")
async def git_upload_pack(
    namespace: str,
    name: str,
    request: Request,
    repo_type: str | None = None,
    user: User | None = Depends(get_git_user),
):
    """Git upload-pack endpoint for clone/fetch/pull."""
    repo_id = f"{namespace}/{name}"
    logger.info(f"Git upload-pack for {repo_id}")

    repo = _authorize_read(_find_repository(repo_type, namespace, name), user)

    request_body = await request.body()

    bridge = GitLakeFSBridge(
        repo.repo_type, namespace, name, lakefs_repo=resolve_lakefs_repo(repo)
    )
    handler = GitUploadPackHandler(repo_id, bridge=bridge)
    response_data = await handler.handle_upload_pack(request_body)

    return Response(
        content=response_data,
        media_type="application/x-git-upload-pack-result",
        headers={"Cache-Control": "no-cache"},
    )


@_routes("post", "/git-receive-pack")
async def git_receive_pack(
    namespace: str,
    name: str,
    request: Request,
    repo_type: str | None = None,
    user: User | None = Depends(get_git_user),
):
    """Git receive-pack endpoint for push (not implemented: always 501)."""
    logger.info(f"Git receive-pack for {namespace}/{name}")

    _authorize_write(_find_repository(repo_type, namespace, name), user)

    # Pushing over HTTP is not implemented: GitReceivePackHandler only parses the
    # ref updates and answers "ok" without storing the pack, so a push looked
    # like it worked while its data was dropped. Refuse it up front, and do not
    # read the body: the pack is neither stored nor needed, and parsing it as
    # pkt-lines kept the worker busy for as long as the push was large.
    raise GitProtocolError(
        501,
        GIT,
        "Pushing over HTTP is not supported yet; use the Hub API "
        "(huggingface_hub) or the web UI to change this repository",
    )


@_routes("get", "/HEAD")
async def git_head(
    namespace: str,
    name: str,
    repo_type: str | None = None,
    user: User | None = Depends(get_git_user),
):
    """Git HEAD endpoint: the default branch reference."""
    _authorize_read(_find_repository(repo_type, namespace, name), user)

    return Response(
        content=b"ref: refs/heads/main\n",
        media_type="text/plain",
    )
