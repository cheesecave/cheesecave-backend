"""HuggingFace Hub API compatibility utilities.

This module provides utilities for making Kohaku Hub compatible with
`huggingface_hub` client behavior.
"""

from typing import Optional

from fastapi import HTTPException
from fastapi.responses import Response
from peewee import PeeweeException

from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import ref_in_history

logger = get_logger("HF")


class HFErrorCode:
    """HuggingFace error codes for X-Error-Code header.

    These error codes are read by huggingface_hub client's hf_raise_for_status()
    function to provide specific error types.

    HuggingFace Hub officially supports the four codes below — these are
    the ones that ``hf_raise_for_status`` dispatches into named exception
    classes (``RepositoryNotFoundError``, ``RevisionNotFoundError``,
    ``EntryNotFoundError``, ``GatedRepoError``). **Do not rename them.**

    Note on the ``DisabledRepo`` case: HF signals it via the
    ``X-Error-Message`` string ``"Access to this resource is disabled."``
    rather than an ``X-Error-Code``; the helper for that lives in
    ``hf_disabled_repo`` below and does not appear in this enum.

    The remaining codes below are **KohakuHub-only extensions**.
    ``hf_raise_for_status`` does not special-case them; downstream
    ``huggingface_hub`` clients surface them as generic
    ``HfHubHTTPError``. They exist so KohakuHub's own UI / SPA / admin
    tooling can branch on a stable code rather than parsing free-text
    messages. They are emitted on the wire — clients that don't expect
    them simply ignore the header.

    Reference: huggingface_hub/utils/_http.py
    """

    # HuggingFace official error codes (DO NOT CHANGE — these drive
    # named-exception dispatch in ``hf_raise_for_status``).
    REPO_NOT_FOUND = "RepoNotFound"
    REVISION_NOT_FOUND = "RevisionNotFound"
    ENTRY_NOT_FOUND = "EntryNotFound"
    GATED_REPO = "GatedRepo"

    # KohakuHub-only extensions (not recognized by hf_raise_for_status —
    # downstream HF clients see these as generic HfHubHTTPError; our SPA
    # and admin tooling key off them for branching).
    REPO_EXISTS = "RepoExists"
    # A repo id whose LakeFS repository is still being deleted. Informational
    # only: hf_raise_for_status has no 409 branch, so HF clients key off the
    # response body instead (see LAKEFS_CONFLICT_RETRY_MESSAGE).
    REPO_NAME_RECYCLING = "RepoNameRecycling"
    BAD_REQUEST = "BadRequest"
    INVALID_REPO_TYPE = "InvalidRepoType"
    INVALID_REPO_ID = "InvalidRepoId"
    SERVER_ERROR = "ServerError"
    NOT_IMPLEMENTED = "NotImplemented"
    UNAUTHORIZED = "Unauthorized"
    FORBIDDEN = "Forbidden"
    RANGE_NOT_SATISFIABLE = "RangeNotSatisfiable"


def _sanitize_header_value(value: str) -> str:
    """Normalize header values so HTTP servers can emit them safely."""
    return " ".join(str(value).split())


def hf_error_response(
    status_code: int,
    error_code: str,
    message: str,
    headers: Optional[dict] = None,
) -> Response:
    """Create HuggingFace-compatible error response.

    HuggingFace client reads error information from HTTP headers, not from response body:
    - X-Error-Code: Specific error code (see HFErrorCode class)
    - X-Error-Message: Human-readable error message

    The response body should be empty. The client's hf_raise_for_status() function
    parses these headers to throw appropriate exceptions like:
    - RepositoryNotFoundError
    - RevisionNotFoundError
    - GatedRepoError
    - EntryNotFoundError
    - etc.

    Args:
        status_code: HTTP status code (404, 403, 400, 500, etc.)
        error_code: HuggingFace error code (use HFErrorCode constants)
        message: Human-readable error message
        headers: Additional headers to include

    Returns:
        Response with proper error headers and empty body

    Examples:
        >>> # Repository not found (404)
        >>> return hf_error_response(
        ...     404,
        ...     HFErrorCode.REPO_NOT_FOUND,
        ...     "Repository 'owner/repo' not found"
        ... )

        >>> # Gated repository (403)
        >>> return hf_error_response(
        ...     403,
        ...     HFErrorCode.GATED_REPO,
        ...     "You need to accept terms to access this repository"
        ... )

        >>> # Revision not found (404)
        >>> return hf_error_response(
        ...     404,
        ...     HFErrorCode.REVISION_NOT_FOUND,
        ...     "Revision 'v1.0' not found"
        ... )
    """
    response_headers = {
        "X-Error-Code": error_code,
        "X-Error-Message": _sanitize_header_value(message),
    }
    if headers:
        response_headers.update(
            {key: _sanitize_header_value(value) for key, value in headers.items()}
        )

    # Return empty body with error in headers
    # HuggingFace client reads from headers, not body
    return Response(
        status_code=status_code,
        headers=response_headers,
    )


def hf_repo_not_found(repo_id: str, repo_type: Optional[str] = None) -> Response:
    """Shortcut for repository not found error (404).

    Args:
        repo_id: Repository ID (e.g., "owner/repo")
        repo_type: Optional repository type for more specific message

    Returns:
        404 response with RepoNotFound error code
    """
    type_str = f" ({repo_type})" if repo_type else ""
    return hf_error_response(
        404,
        HFErrorCode.REPO_NOT_FOUND,
        f"Repository '{repo_id}'{type_str} not found",
    )


def hf_disabled_repo(repo_id: Optional[str] = None) -> Response:
    """Shortcut for "this repository is disabled" error (403).

    HuggingFace flags moderation-disabled repositories with an exact
    ``X-Error-Message`` string — ``"Access to this resource is disabled."``
    — and **no** ``X-Error-Code`` header. ``huggingface_hub.utils
    ._http.hf_raise_for_status`` dispatches ``DisabledRepoError`` by
    matching that exact message string (verified live against
    ``huggingface_hub`` 1.11.0); changing the casing or punctuation
    breaks the dispatch.

    No call site wires this helper today — ``DisabledRepoError`` is
    reserved for a future moderation feature ("admin disables a repo")
    and the helper is added here so the wire shape is centralized when
    that feature lands. Keep the message string verbatim.

    Args:
        repo_id: Optional repository id, included in our
            ``X-Khub-Repo`` header (debug aid for operators); the
            HF-canonical message stays exact.

    Returns:
        403 response with HF's exact ``X-Error-Message``, no
        ``X-Error-Code``, empty body.
    """
    extra: dict[str, str] = {}
    if repo_id:
        extra["X-Khub-Repo"] = repo_id
    response = Response(
        status_code=403,
        headers={
            # HF's exact wire string — DisabledRepoError's dispatch is a
            # whole-string match, so this must not be paraphrased.
            "X-Error-Message": "Access to this resource is disabled.",
            **extra,
        },
    )
    return response


def hf_gated_repo(repo_id: str, message: Optional[str] = None) -> Response:
    """Shortcut for gated repository error (403).

    Args:
        repo_id: Repository ID
        message: Optional custom message

    Returns:
        403 response with GatedRepo error code
    """
    if message is None:
        message = (
            f"Repository '{repo_id}' is gated. "
            "You need to accept the terms to access it."
        )

    return hf_error_response(
        403,
        HFErrorCode.GATED_REPO,
        message,
    )


def hf_revision_not_found(
    repo_id: str,
    revision: str,
) -> Response:
    """Shortcut for revision not found error (404).

    Args:
        repo_id: Repository ID
        revision: Revision/branch name that was not found

    Returns:
        404 response with RevisionNotFound error code
    """
    return hf_error_response(
        404,
        HFErrorCode.REVISION_NOT_FOUND,
        f"Revision '{revision}' not found in repository '{repo_id}'",
    )


async def ensure_revision_in_history(client, repo, lakefs_repo: str, revision: str) -> None:
    """404 RevisionNotFound for a commit a squash of ``repo`` removed
    (``kohakuhub.utils.lakefs.in_history``)."""
    if not await ref_in_history(client, repo, lakefs_repo, revision):
        message = f"Revision '{revision}' not found in repository '{repo.full_id}'"
        raise HTTPException(
            status_code=404,
            detail={"error": message},
            headers={"X-Error-Code": HFErrorCode.REVISION_NOT_FOUND, "X-Error-Message": message},
        )


def hf_entry_not_found(
    repo_id: str,
    path: str,
    revision: Optional[str] = None,
) -> Response:
    """Shortcut for file/entry not found error (404).

    Args:
        repo_id: Repository ID
        path: File path that was not found
        revision: Optional revision/branch name

    Returns:
        404 response with EntryNotFound error code
    """
    revision_str = f" at revision '{revision}'" if revision else ""
    return hf_error_response(
        404,
        HFErrorCode.ENTRY_NOT_FOUND,
        f"Entry '{path}' not found in repository '{repo_id}'{revision_str}",
    )


def hf_bad_request(message: str) -> Response:
    """Shortcut for bad request error (400).

    Args:
        message: Error message

    Returns:
        400 response with BadRequest error code
    """
    return hf_error_response(
        400,
        HFErrorCode.BAD_REQUEST,
        message,
    )


def hf_server_error(message: str, error_code: Optional[str] = None) -> Response:
    """Shortcut for server error (500).

    Args:
        message: Error message
        error_code: Optional custom error code (defaults to ServerError)

    Returns:
        500 response with ServerError error code
    """
    return hf_error_response(
        500,
        error_code or HFErrorCode.SERVER_ERROR,
        message,
    )


def hf_not_implemented(
    feature: str,
    reason: Optional[str] = None,
) -> Response:
    """Shortcut for "feature not supported" error (501).

    HuggingFace's `hf_raise_for_status` does not special-case the `NotImplemented`
    error code, so the client surfaces this as a plain `HfHubHTTPError`. The
    `X-Error-Message` header drives the exception's `server_message` so the
    user sees our reason text in their traceback — keep it specific and
    actionable.

    Args:
        feature: Short name of the feature that was requested
            (e.g. "create_pr", "discussions", "space runtime").
        reason: Optional additional explanation to append to the message.

    Returns:
        501 response with ``X-Error-Code: NotImplemented``.
    """
    message = f"{feature} is not supported by KohakuHub"
    if reason:
        message = f"{message}. {reason}"

    return hf_error_response(
        501,
        HFErrorCode.NOT_IMPLEMENTED,
        message,
    )


def hf_unauthorized(message: str) -> Response:
    """Shortcut for unauthenticated error (401)."""
    return hf_error_response(
        401,
        HFErrorCode.UNAUTHORIZED,
        message,
    )


def hf_forbidden(message: str) -> Response:
    """Shortcut for forbidden error (403).

    HuggingFace's client formats 403 responses as
    ``"403 Forbidden: {error_message}."`` — the X-Error-Message string we
    send is interpolated into that template verbatim, so keep it phrased as
    a noun phrase (e.g. "write access required") rather than a sentence.
    """
    return hf_error_response(
        403,
        HFErrorCode.FORBIDDEN,
        message,
    )


def hf_range_not_satisfiable(
    total_size: int,
    requested_range: Optional[str] = None,
) -> Response:
    """Shortcut for Range-not-satisfiable error (416).

    HuggingFace's client reads ``Content-Range`` on 416 to enrich the
    error message, so we must emit it here.
    """
    headers = {"Content-Range": f"bytes */{total_size}"}
    detail = (
        f"Requested range '{requested_range}' is not satisfiable"
        if requested_range
        else "Requested range is not satisfiable"
    )
    return hf_error_response(
        416,
        HFErrorCode.RANGE_NOT_SATISFIABLE,
        detail,
        headers=headers,
    )


async def collect_hf_siblings(
    repo_row,
    repo_type: str,
    repo_id: str,
    revision: str,
    *,
    with_metadata: bool = True,
) -> list[dict]:
    """Collect repository files using the schema expected by `huggingface_hub`.

    Args:
        with_metadata: When False, emit only ``rfilename`` per file — no
            ``size``, no ``lfs`` block — and skip the File-table load entirely.
            This is what the ``blobs`` query parameter maps to; HF's own default
            response is name-only, and on a large repo the metadata is what
            makes this expensive (4000 files: 0.102s and 449KB with it, 0.037s
            and 160KB without).
    """
    from kohakuhub.db_operations import get_repo_file_sha256_map, should_use_lfs
    from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

    lakefs_repo = resolve_lakefs_repo(repo_row)
    client = get_lakefs_client()
    all_results = []
    after = ""

    while True:
        result = await client.list_objects(
            repository=lakefs_repo,
            ref=revision,
            prefix="",
            delimiter="",
            amount=1000,
            after=after,
        )

        if isinstance(result, list):
            all_results.extend(result)
            break

        all_results.extend(result.get("results", []))
        pagination = result.get("pagination", {})
        if not pagination.get("has_more"):
            break

        after = pagination.get("next_offset")
        if not after:
            break

    file_objects = [obj for obj in all_results if obj.get("path_type") == "object"]

    if not with_metadata:
        # Name-only: no File rows needed, no size/lfs fields emitted.
        return [{"rfilename": obj["path"]} for obj in file_objects]

    # One query for the whole repo instead of one per LFS file: at 4000 files
    # the per-path loop costs 2.137s against 0.102s for this whole function.
    try:
        file_sha256 = get_repo_file_sha256_map(repo_row)
    except PeeweeException as e:
        # Keep serving the listing rather than failing the repo page, but say so
        # loudly: unlike the previous per-path lookup, one failure here drops the
        # stored sha256 for *every* file, and the LakeFS checksum substituted
        # below is an ETag-shaped value rather than a sha256. Degrading silently
        # would hand clients plausible-but-wrong LFS oids at HTTP 200.
        logger.warning(
            f"Could not load File rows for {repo_id}; LFS sha256 falls back to"
            f" LakeFS checksums for every file: {e}"
        )
        file_sha256 = {}

    siblings = []
    for obj in file_objects:
        path = obj["path"]
        size = obj.get("size_bytes", 0)
        sibling = {
            "rfilename": path,
            "size": size,
        }

        if should_use_lfs(repo_row, path, size):
            # Pre-existing fallback, unchanged: a path with no File row uses
            # the LakeFS checksum, even though that field is documented as
            # "typically ETag" and can carry a `sha256:` prefix. Whether that
            # is the right value is a separate correctness question.
            checksum = file_sha256.get(path) or obj.get("checksum", "")
            sibling["lfs"] = {
                "sha256": checksum,
                "size": size,
                "pointerSize": 134,
            }

        siblings.append(sibling)

    return siblings


def format_hf_datetime(dt) -> Optional[str]:
    """Format datetime for HuggingFace API responses.

    Handles both datetime objects and string timestamps from database.

    Args:
        dt: datetime object, string timestamp, or None

    Returns:
        ISO format datetime string with milliseconds or None

    Example:
        >>> from datetime import datetime
        >>> dt = datetime(2025, 1, 15, 10, 30, 45)
        >>> format_hf_datetime(dt)
        '2025-01-15T10:30:45.000000Z'
    """
    if dt is None:
        return None

    # Import here to avoid circular dependency
    from kohakuhub.utils.datetime_utils import safe_strftime

    # HuggingFace format: "2025-01-15T10:30:45.123456Z"
    return safe_strftime(dt, "%Y-%m-%dT%H:%M:%S.%fZ")


def is_lakefs_not_found_error(error: Exception) -> bool:
    """Check if an exception is a LakeFS not found error.

    Args:
        error: Exception to check

    Returns:
        True if the error indicates a 404/not found condition
    """
    error_str = str(error).lower()
    return "404" in error_str or "not found" in error_str


def is_lakefs_revision_error(error: Exception) -> bool:
    """Check if an exception is a LakeFS revision/branch error.

    Args:
        error: Exception to check

    Returns:
        True if the error is related to revision/branch
    """
    error_str = str(error).lower()
    return "revision" in error_str or "branch" in error_str or "ref" in error_str
