"""Repository information and listing endpoints."""

import json
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from peewee import JOIN, fn

from kohakuhub.config import cfg
from kohakuhub.constants import DATETIME_FORMAT_ISO
from kohakuhub import repository_discovery as discovery
from kohakuhub.db import Commit, Repository, RepositoryCard, User
from kohakuhub.db_operations import (
    get_merged_external_tokens,
    get_organization,
    get_repository,
    get_user_by_username,
)
from kohakuhub.logger import get_logger
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.auth.permissions import (
    check_repo_read_permission,
    check_repo_write_permission,
    filter_readable_repositories,
)
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo
from kohakuhub.api.fallback import with_repo_fallback, with_user_fallback
from kohakuhub.api.fallback.config import get_enabled_sources
from kohakuhub.api.fallback.operations import ExternalListError
from kohakuhub.api.repo.utils import listing
from kohakuhub.api.utils.paging import next_link
from kohakuhub.utils.datetime_utils import safe_strftime
from kohakuhub.utils.commit_id import hub_oid
from kohakuhub.api.utils.trending import calculate_trending_scores
from kohakuhub.api.repo.utils.hf import (
    HFErrorCode,
    CARD_PROPERTIES,
    expand_error,
    hf_error_response,
    hf_repo_info_response,
    hf_repo_not_found,
    hf_siblings_json,
    repo_info_fields,
)

logger = get_logger("REPO")
router = APIRouter()

RepoType = Literal["model", "dataset", "space"]


def _latest_main_commits(repo_ids: list[int]) -> dict[int, tuple[str, datetime]]:
    """Resolve the latest main-branch commit for each repo via the local DB.

    Returns ``{repo_id: (commit_id, created_at)}`` for repos that have at least
    one ``branch == "main"`` row in the ``Commit`` table. Repos that don't
    appear in the ``Commit`` table are simply absent from the result — list
    callers fall back to LakeFS for those (e.g. freshly created repos with no
    HF-API commits, repos populated only via ``git push`` or ``repo rename``,
    which currently bypass ``create_commit``).

    Replaces what used to be two LakeFS REST round-trips per row
    (``get_branch`` + ``get_commit``) with a single SQL aggregate. See issue
    #62 for the perf analysis.
    """
    if not repo_ids:
        return {}

    # Two-pass to stay portable across SQLite (test fixture) and PostgreSQL
    # (prod): first compute MAX(created_at) per repo, then look up the matching
    # row. PostgreSQL's DISTINCT ON would be tighter but isn't available on
    # SQLite. The (repository, branch) composite index already exists; the
    # MAX(created_at) sort is the part that benefits from the index proposed
    # in issue #68 — measurable but not blocking.
    latest_subq = (
        Commit.select(
            Commit.repository.alias("rid"),
            fn.MAX(Commit.created_at).alias("max_at"),
        )
        .where((Commit.repository.in_(repo_ids)) & (Commit.branch == "main"))
        .group_by(Commit.repository)
        .alias("latest")
    )

    rows = (
        Commit.select(Commit.repository, Commit.commit_id, Commit.created_at)
        .join(
            latest_subq,
            on=(
                (Commit.repository == latest_subq.c.rid)
                & (Commit.created_at == latest_subq.c.max_at)
            ),
        )
        .where(Commit.branch == "main")
    )

    out: dict[int, tuple[str, datetime]] = {}
    for r in rows:
        # ``created_at`` ties (sub-microsecond commits) collapse to the first
        # row encountered — fine, they share a timestamp by definition.
        out.setdefault(r.repository_id, (r.commit_id, r.created_at))
    return out


async def _resolve_main_head_via_lakefs(
    client, lakefs_repo: str
) -> tuple[str | None, str | None]:
    """LakeFS-side fallback for repos missing from the DB ``Commit`` table.

    Returns ``(sha, last_modified_iso)`` or ``(None, None)`` on any error.
    Kept as a small helper so the list callsites can stay readable.
    """
    try:
        branch = await client.get_branch(repository=lakefs_repo, branch="main")
    except Exception as e:
        logger.debug(f"get_branch fallback failed for {lakefs_repo}: {e}")
        return None, None

    sha = branch.get("commit_id")
    if not sha:
        return None, None

    try:
        commit_info = await client.get_commit(
            repository=lakefs_repo, commit_id=sha
        )
    except Exception as e:
        logger.debug(f"get_commit fallback failed for {lakefs_repo}: {e}")
        return sha, None

    last_modified: str | None = None
    if commit_info and commit_info.get("creation_date"):
        last_modified = datetime.fromtimestamp(
            commit_info["creation_date"]
        ).strftime(DATETIME_FORMAT_ISO)
    return sha, last_modified


def _apply_repo_sorting(q, repo_type: str, sort: str):
    """Apply repository sorting while preserving existing API semantics."""
    if sort == "likes":
        return q.order_by(Repository.likes_count.desc())

    if sort == "downloads":
        return q.order_by(Repository.downloads.desc())

    if sort == "updated":
        latest_commit_subq = (
            Commit.select(
                Commit.repository.alias("repository_id"),
                fn.MAX(Commit.created_at).alias("last_commit_at"),
            )
            .where((Commit.repo_type == repo_type) & (Commit.branch == "main"))
            .group_by(Commit.repository)
            .alias("latest_commit_subq")
        )

        return q.join(
            latest_commit_subq,
            JOIN.LEFT_OUTER,
            on=(Repository.id == latest_commit_subq.c.repository_id),
        ).order_by(
            fn.COALESCE(
                latest_commit_subq.c.last_commit_at,
                Repository.created_at,
            ).desc(),
            Repository.created_at.desc(),
        )

    return q.order_by(Repository.created_at.desc())


def _bracket_expand(request) -> list[str]:
    """The ``expand[]=`` values of a request. Callers other than the server hand the route a request
    with its query as a plain mapping (the admin probe), or with none at all."""
    params = getattr(request, "query_params", None)
    if params is None:
        return []
    if hasattr(params, "getlist"):
        return params.getlist("expand[]")
    value = params.get("expand[]")
    return [value] if isinstance(value, str) else list(value or [])


@router.get("/models/{namespace}/{repo_name}")
@router.get("/datasets/{namespace}/{repo_name}")
@router.get("/spaces/{namespace}/{repo_name}")
@with_repo_fallback("info")
async def get_repo_info(
    namespace: str,
    repo_name: str,
    request: Request,
    fallback: bool = True,
    blobs: bool = False,
    expand: Optional[list[str]] = Query(None),
    user: User | None = Depends(get_optional_user),
):
    """Get repository information (without revision).

    This endpoint matches HuggingFace Hub API format:
    - /api/models/{namespace}/{repo_name}
    - /api/datasets/{namespace}/{repo_name}
    - /api/spaces/{namespace}/{repo_name}

    Note: For revision-specific info, use /{repo_type}s/{namespace}/{repo_name}/revision/{revision}
          which is handled in files.py

    Args:
        namespace: Repository namespace (user or organization)
        repo_name: Repository name
        request: FastAPI request object
        user: Current authenticated user (optional)

    Returns:
        Repository metadata or error response with headers
    """
    # Construct full repo ID
    repo_id = f"{namespace}/{repo_name}"

    # Determine repo type from path
    path = request.url.path
    match path:
        case _ if "/models/" in path:
            repo_type = "model"
        case _ if "/datasets/" in path:
            repo_type = "dataset"
        case _ if "/spaces/" in path:
            repo_type = "space"
        case _:
            return hf_error_response(
                404,
                HFErrorCode.INVALID_REPO_TYPE,
                "Invalid repository type",
            )

    # Check if repository exists in database
    repo_row = get_repository(repo_type, namespace, repo_name)

    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    # Hugging Face Hub hides private repos from unauthorized users.
    # ``check_repo_read_permission`` raises ``RepoReadDeniedError`` for
    # both anonymous-on-private and authed-no-access; the global handler
    # in ``main.py`` converts that to ``hf_repo_not_found(...)``.
    check_repo_read_permission(repo_row, user)

    # As on the Hub: an unknown ``expand`` property is a 400
    # ``expand=`` and ``expand[]=`` both spell it, and an empty ``expand=`` asks for none, as on the Hub
    expand = [prop for prop in (expand or []) + _bracket_expand(request) if prop] or None
    if bad := expand_error(repo_type, expand):
        return bad

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()
    commit_id = None
    last_modified = None
    try:
        branch = await client.get_branch(repository=lakefs_repo, branch="main")
        commit_id = branch["commit_id"]
        if commit_id:
            try:
                commit_info = await client.get_commit(
                    repository=lakefs_repo, commit_id=commit_id
                )
                if commit_info and commit_info.get("creation_date"):
                    last_modified = datetime.fromtimestamp(
                        commit_info["creation_date"]
                    ).strftime(DATETIME_FORMAT_ISO)
            except Exception as ex:
                logger.debug(f"Could not get commit info: {str(ex)}")
    except Exception as e:
        # Log warning but continue - repo exists even if LakeFS has issues
        logger.warning(f"Could not get branch info for {lakefs_repo}/main: {str(e)}")

    # The file list, only when it is part of the answer: by default, when
    # ``expand`` names it, or with ``blobs`` (the Hub adds it then too). It
    # lists the whole repository, so a caller after the page fields alone
    # (``expand=sha&expand=lastModified...``) costs nothing per file.
    siblings = None
    if not expand or "siblings" in expand or blobs:
        siblings = "[]"
        if commit_id:
            try:
                siblings = await hf_siblings_json(
                    repo_row, lakefs_repo, commit_id, with_metadata=blobs
                )
            except Exception as ex:
                logger.exception(f"Could not fetch siblings for {lakefs_repo}: {str(ex)}", ex)

    card = None
    if not expand or CARD_PROPERTIES & set(expand):
        card = await discovery.info_card(repo_row, head=commit_id)
    fields = repo_info_fields(
        repo_row,
        repo_type,
        hub_oid(commit_id),
        last_modified,
        storage=bool(user) and (not expand or "storage" in expand),
        card=card,
    )
    return hf_repo_info_response(fields, expand, siblings)


@router.get("/models/{namespace}/{repo_name}/auth-check")
@router.get("/datasets/{namespace}/{repo_name}/auth-check")
@router.get("/spaces/{namespace}/{repo_name}/auth-check")
@with_repo_fallback("info")
async def repo_auth_check(
    namespace: str,
    repo_name: str,
    request: Request,
    fallback: bool = True,
    user: User | None = Depends(get_optional_user),
):
    """``HfApi.auth_check``: 200 when the caller can read the repository.

    Anything else is the same 404 ``RepoNotFound`` the info route answers, so a private
    repository is not told apart from a missing one (the client raises
    ``RepositoryNotFoundError`` for both).
    """
    repo_type = request.url.path.split("/")[-4].removesuffix("s")
    repo_row = get_repository(repo_type, namespace, repo_name)
    if not repo_row:
        return hf_repo_not_found(f"{namespace}/{repo_name}", repo_type)
    check_repo_read_permission(repo_row, user)
    return {}


def _filter_repos_by_privacy(q, user: Optional[User], author: Optional[str] = None):
    """Compatibility wrapper for older internal callers; policy lives in auth."""
    return filter_readable_repositories(q, user, author)


async def _render_local(ids: list[int], tag_source: bool, with_card: bool = False) -> list[dict]:
    """The list items of the repositories ``ids``, in that order.

    sha + lastModified come from one SQL aggregate; LakeFS answers only for repos missing
    from the Commit table (issue #62).
    """
    if not ids:
        return []
    by_id = {r.id: r for r in Repository.select().where(Repository.id.in_(ids))}
    # The last known card, even while a newer commit is being indexed: a list shows what was true a
    # moment ago rather than nothing (the info route is the one that waits for the head it answers)
    indexed = {
        row.repository_id: row
        for row in RepositoryCard.select(
            RepositoryCard.repository, RepositoryCard.derived, *([RepositoryCard.card] if with_card else [])
        ).where(RepositoryCard.repository.in_(ids))
    }
    heads = _latest_main_commits(ids)
    client = get_lakefs_client()
    # One query per kind on the page, for the page's own rows
    trending: dict[int, float] = {}
    for kind in {r.repo_type for r in by_id.values()}:
        trending.update(calculate_trending_scores(kind, ids=[r.id for r in by_id.values() if r.repo_type == kind]))
    result = []
    for repo_id in ids:
        r = by_id.get(repo_id)
        if r is None:  # deleted since the order was read
            continue
        head = heads.get(r.id)
        if head is not None:
            sha, last_at = head
            last_modified = safe_strftime(last_at, DATETIME_FORMAT_ISO)
        else:
            sha, last_modified = await _resolve_main_head_via_lakefs(
                client, resolve_lakefs_repo(r)
            )
        item = {
            "_id": str(r.id),
            "id": r.full_id,
            "author": r.namespace,
            "private": r.private,
            "sha": hub_oid(sha),
            "lastModified": last_modified,
            "createdAt": safe_strftime(r.created_at, DATETIME_FORMAT_ISO),
            "downloads": r.downloads,
            "likes": r.likes_count,
            "gated": False,
            "trendingScore": round(trending.get(r.id, 0), 2),
            "tags": [],
        }
        if r.repo_type == "model":
            item["modelId"] = r.full_id
        elif r.repo_type == "dataset":
            item["disabled"] = False
        row = indexed.get(r.id)
        if row is not None:
            declared = json.loads(row.derived)
            item["tags"] = declared["tags"]
            if r.repo_type == "model":
                item["pipeline_tag"] = declared["pipeline_tag"]
                item["library_name"] = declared["library_name"]
            elif r.repo_type == "space":
                item["sdk"] = declared["sdk"]
            if with_card and (card := json.loads(row.card or "{}")):
                item["cardData"] = card
        if tag_source:
            item["_source"] = "local"
            item["_source_url"] = cfg.app.base_url
        result.append(item)
    return result


@router.get("/models")
@router.get("/datasets")
@router.get("/spaces")
async def list_repos(
    request: Request,
    author: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1),
    sort: Optional[str] = None,
    direction: Optional[int] = None,
    search: Optional[str] = None,
    cursor: Optional[str] = None,
    partial: Optional[str] = None,
    cardData: bool = False,
    full: bool = False,
    fallback: bool = Query(True, description="Enable fallback to external sources"),
    user: User | None = Depends(get_optional_user),
):
    """List repositories like the Hub: a bare array, the next page in ``Link``.

    Unknown parameters are ignored. See ``kohakuhub.api.repo.utils.listing`` for the model.
    """
    repo_type = request.url.path.rsplit("/", 1)[-1].removesuffix("s")
    params = request.query_params
    try:
        query = listing.ListQuery(
            repo_type=repo_type,
            author=author,
            sort=listing.parse_sort(sort),
            ascending=listing.parse_direction(direction),
            search=search or None,
            tags=tuple(params.getlist("filter")),
            pipeline_tag=tuple(params.getlist("pipeline_tag")),
            library=tuple(params.getlist("library")),
            language=tuple(params.getlist("language")),
            task_categories=tuple(params.getlist("task_categories")),
            size_categories=tuple(params.getlist("size_categories")),
            page_size=listing.page_size_for(limit),
            partial=partial == "ok",
        )
        state = listing.decode_cursor(cursor, query.fingerprint()) if cursor else None
    except (listing.ListParamError, listing.CursorError) as error:
        return hf_error_response(400, HFErrorCode.BAD_REQUEST, str(error))

    sources = []
    if cfg.fallback.enabled and fallback:
        header_tokens = getattr(request.state, "external_tokens", {})
        sources = get_enabled_sources(
            namespace=author or "",
            user_tokens=get_merged_external_tokens(user, header_tokens),
        )

    try:
        page = await listing.build_page(
            query,
            user,
            state=state,
            sources=sources,
            render_local=lambda ids: _render_local(ids, tag_source=bool(sources), with_card=cardData or full),
        )
    except listing.CursorError as error:
        return hf_error_response(400, HFErrorCode.BAD_REQUEST, str(error))
    except ExternalListError as error:
        return hf_error_response(
            503,
            HFErrorCode.SERVER_ERROR,
            f"{error.source} could not list {repo_type}s ({error.reason}); retry, or pass partial=ok to skip it",
            headers={"Retry-After": "2"},
        )

    headers = {}
    if page.next_state is not None:
        token = listing.encode_cursor(page.next_state, query.fingerprint())
        headers["Link"] = f'<{next_link(request, token)}>; rel="next"'
    if page.incomplete:
        headers["X-Cheesecave-Incomplete"] = ", ".join(page.incomplete)
    return JSONResponse(page.items, headers=headers)


@router.get("/users/{username}/repos")
@with_user_fallback("repos")
async def list_user_repos(
    username: str,
    request: Request,
    limit: int = Query(100, ge=1, le=100000),  # Very high limit to support "get all repos"
    sort: str = Query("recent", pattern="^(recent|updated|likes|downloads)$"),
    fallback: bool = True,
    user: User | None = Depends(get_optional_user),
):
    """List all repositories for a specific user/namespace.

    This endpoint returns repositories grouped by type, similar to a profile page.

    Args:
        username: Username or organization name
        limit: Maximum number of results per type
        sort: Sort order (recent, updated, likes, downloads) - default: recent
        user: Current authenticated user (optional)

    Returns:
        Dict with models, datasets, and spaces lists
    """
    # Check if the username exists
    target_user = get_user_by_username(username)
    if not target_user:
        # Could also be an organization
        target_org = get_organization(username)
        if not target_org:
            return hf_error_response(
                404,
                HFErrorCode.BAD_REQUEST,
                f"User or organization '{username}' not found",
            )

    result = {
        "models": [],
        "datasets": [],
        "spaces": [],
    }

    for repo_type in ["model", "dataset", "space"]:
        q = Repository.select().where(
            (Repository.repo_type == repo_type) & (Repository.namespace == username)
        )

        # Apply privacy filtering
        q = filter_readable_repositories(q, user)

        # Reuse the list ordering, including main-branch activity before limiting.
        rows = list(_apply_repo_sorting(q, repo_type, sort).limit(limit))

        key = repo_type + "s"
        # Same SQL-first / LakeFS-fallback shape as _list_repos_internal.
        heads = _latest_main_commits([r.id for r in rows])
        client = get_lakefs_client()
        repos_list = []

        for r in rows:
            head = heads.get(r.id)
            if head is not None:
                sha, last_at = head
                last_modified = safe_strftime(last_at, DATETIME_FORMAT_ISO)
            else:
                lakefs_repo = resolve_lakefs_repo(r)
                sha, last_modified = await _resolve_main_head_via_lakefs(
                    client, lakefs_repo
                )

            repos_list.append(
                {
                    "id": r.full_id,
                    "author": r.namespace,
                    "private": r.private,
                    "sha": hub_oid(sha),
                    "lastModified": last_modified,
                    "createdAt": safe_strftime(r.created_at, DATETIME_FORMAT_ISO),
                    "downloads": r.downloads,
                    "likes": r.likes_count,
                    "gated": False,
                    "tags": [],
                }
            )

        # Sorting already applied by database query
        result[key] = repos_list

    return result
