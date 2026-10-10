"""HuggingFace Hub API compatibility utilities.

This module provides utilities for making Kohaku Hub compatible with
`huggingface_hub` client behavior.
"""

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import Response
from peewee import PeeweeException

from kohakuhub.db import File
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


# The properties the Hub accepts in a repository info ``expand``, per type
# (its own validation message, 2026-10). ``storage`` is KohakuHub's: the
# repository's quota usage.
_EXPAND_COMMON = {
    "author", "cardData", "createdAt", "disabled", "lastModified", "likes", "private",
    "resourceGroup", "sha", "siblings", "tags", "trendingScore", "usedStorage", "xetEnabled",
    "storage",
}
EXPAND_PROPERTIES = {
    "model": frozenset(_EXPAND_COMMON | {
        "baseModels", "childrenModelCount", "config", "downloads", "downloadsAllTime",
        "evalResults", "gated", "gguf", "inference", "inferenceProviderMapping",
        "library_name", "mask_token", "model-index", "pipeline_tag", "safetensors",
        "spaces", "transformersInfo", "widgetData",
    }),
    "dataset": frozenset(_EXPAND_COMMON | {
        "citation", "description", "downloads", "downloadsAllTime", "gated", "mainSize",
        "paperswithcode_id",
    }),
    "space": frozenset(_EXPAND_COMMON | {
        "datasets", "models", "region", "runtime", "sdk", "subdomain",
    }),
}

# Properties the Hub returns only when expanded, from another field
_EXPAND_ONLY = {"downloadsAllTime": "downloads"}

# Name-only sibling lists, per (LakeFS repository, commit, False): a
# commit's file list never changes. ponytail: per process, by bytes; share it through
# Valkey if several workers keep rebuilding the same big lists.
MANIFEST_CACHE_BYTES = 256 * 1024 * 1024
_manifests: "OrderedDict[tuple[str, str, bool], str]" = OrderedDict()
_building: dict[tuple[str, str, bool], asyncio.Task] = {}


def repo_info_fields(
    repo_row, repo_type: str, sha: Optional[str], last_modified: Optional[str], storage: bool
) -> dict:
    """The repository info fields KohakuHub has values for (``siblings`` aside).

    ``storage`` (KohakuHub's quota usage) is computed only when asked: it
    reads the namespace's quota.
    """
    repo_id = f"{repo_row.namespace}/{repo_row.name}"
    fields = {
        "_id": repo_row.id,
        "id": repo_id,
        "modelId": repo_id if repo_type == "model" else None,
        "author": repo_row.namespace,
        "sha": sha,
        "lastModified": last_modified,
        "createdAt": format_hf_datetime(repo_row.created_at),
        "private": repo_row.private,
        "disabled": False,
        "gated": False,
        "downloads": repo_row.downloads,
        "likes": repo_row.likes_count,
        "tags": [],
        "pipeline_tag": None,
        "library_name": None,
        "usedStorage": repo_row.used_bytes,
        "spaces": [],
        "models": [],
        "datasets": [],
    }
    if storage:
        from kohakuhub.api.quota.util import get_repo_storage_info

        try:
            data = get_repo_storage_info(repo_row)
            fields["storage"] = {
                key: data[key]
                for key in ("quota_bytes", "used_bytes", "available_bytes", "percentage_used",
                            "effective_quota_bytes", "is_inheriting")
            }
        except Exception as e:  # the rest of the page still answers
            logger.warning(f"Failed to get storage info for {repo_id}: {e}")
    return fields


def expand_error(repo_type: str, expand: Optional[list[str]]) -> Optional[Response]:
    """The Hub's 400 for a property it does not know, else ``None``."""
    allowed = EXPAND_PROPERTIES[repo_type]
    for prop in expand or []:
        if prop not in allowed:
            options = "|".join(f'"{p}"' for p in sorted(allowed))
            return hf_bad_request(f'Invalid option "{prop}" in expand: expected one of {options}')
    return None


def hf_repo_info_response(
    fields: dict, expand: Optional[list[str]], siblings: Optional[str]
) -> Response:
    """A repository info body as the Hub shapes it.

    Without ``expand``: every field, and ``siblings`` when given. With it:
    ``_id`` and ``id``, then only the asked properties (``None`` for those
    KohakuHub has no value for). ``siblings`` is already JSON, so a big list
    is not walked again by the response encoder.
    """
    if expand:
        body = {"_id": fields["_id"], "id": fields["id"]}
        body.update(
            (prop, fields.get(_EXPAND_ONLY.get(prop, prop))) for prop in expand if prop != "siblings"
        )
    else:
        body = fields
    text = json.dumps(body)
    if siblings is not None:
        text = f'{text[:-1]}{", " if body else ""}"siblings": {siblings}}}'
    return Response(content=text, media_type="application/json")


def git_blob_id(content: bytes) -> str:
    """Git's blob id: the Hub's ``blobId``."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def lfs_pointer(sha256: str, size: int) -> bytes:
    """The git-lfs pointer file git stores for an LFS file."""
    return f"version https://git-lfs.github.com/spec/v1\noid sha256:{sha256}\nsize {size}\n".encode()


async def list_repo_objects(lakefs_repo: str, ref: str) -> list[tuple[str, int, str]]:
    """Every object at ``ref``: (path, size, physical address)."""
    from kohakuhub.utils.lakefs import get_lakefs_client

    client = get_lakefs_client()
    objects = []
    after = ""
    while True:
        result = await client.list_objects(
            repository=lakefs_repo, ref=ref, prefix="", delimiter="", amount=1000, after=after
        )
        page = result if isinstance(result, list) else result.get("results", [])
        objects.extend(
            (obj["path"], obj.get("size_bytes") or 0, obj.get("physical_address") or "")
            for obj in page
            if obj.get("path_type") == "object"
        )
        if isinstance(result, list):
            return objects
        pagination = result.get("pagination", {})
        after = pagination.get("next_offset")
        if not pagination.get("has_more") or not after:
            return objects


# Some write paths keep a LakeFS checksum in File.sha256: not a blobId
_GIT_BLOB_ID = re.compile(r"^[0-9a-f]{40}$")


def _regular_blob_ids(repo_row, branch: str) -> dict[str, str]:
    """Git blob ids of the live regular files of ``branch`` (``File.sha256``)."""
    rows = File.select(File.path_in_repo, File.sha256).where(
        (File.repository == repo_row.id)
        & (File.branch == branch)
        & (File.lfs == False)  # noqa: E712
        & (File.is_deleted == False)  # noqa: E712
    )
    return {row.path_in_repo: row.sha256 for row in rows}


# Both lists are written entry by entry rather than as dicts for json.dumps:
# on 445k files that holds a third less memory (and is faster) for the
# same JSON.
def _names_json(objects: list[tuple[str, int, str]]) -> str:
    dumps = json.dumps
    return "[" + ", ".join(f'{{"rfilename": {dumps(path)}}}' for path, _, _ in objects) + "]"


def _blobs_json(objects: list[tuple[str, int, str]], blob_ids: dict[str, str]) -> str:
    from kohakuhub.lfs_gc import lfs_oid

    dumps = json.dumps
    entries = []
    for path, size, address in objects:
        name = dumps(path)
        oid = lfs_oid(address)
        if oid:
            pointer = lfs_pointer(oid, size)
            entries.append(
                f'{{"rfilename": {name}, "blobId": "{git_blob_id(pointer)}", "size": {size}, '
                f'"lfs": {{"sha256": "{oid}", "size": {size}, "pointerSize": {len(pointer)}}}}}'
            )
        elif _GIT_BLOB_ID.match(blob_ids.get(path, "")):
            entries.append(f'{{"rfilename": {name}, "blobId": "{blob_ids[path]}", "size": {size}}}')
        else:
            entries.append(f'{{"rfilename": {name}, "size": {size}}}')
    return "[" + ", ".join(entries) + "]"


def _remember(key: tuple[str, str, bool], manifest: str) -> None:
    if len(manifest) > MANIFEST_CACHE_BYTES:
        return
    _manifests[key] = manifest
    while sum(map(len, _manifests.values())) > MANIFEST_CACHE_BYTES:
        _manifests.popitem(last=False)


async def _build(repo_row, key: tuple[str, str, bool]) -> str:
    lakefs_repo, commit, with_metadata = key
    objects = await list_repo_objects(lakefs_repo, commit)
    if with_metadata:
        try:
            # The manifest is the default branch's: repository info is repo-level (#11)
            blob_ids = _regular_blob_ids(repo_row, "main")
        except PeeweeException as e:
            logger.warning(f"Could not load File rows for {repo_row.full_id}; regular files get no blobId: {e}")
            blob_ids = {}
        # Shared by concurrent requests, not kept: regular files' blobIds are
        # File rows, which a commit records after LakeFS has it
        return await asyncio.to_thread(_blobs_json, objects, blob_ids)
    manifest = await asyncio.to_thread(_names_json, objects)
    _remember(key, manifest)
    return manifest


async def hf_siblings_json(repo_row, lakefs_repo: str, commit: str, *, with_metadata: bool) -> str:
    """The Hub's ``siblings`` at ``commit``, as JSON.

    Name-only by default; with ``blobs``, ``blobId`` and ``size`` for every
    file, and ``lfs`` for the files LakeFS links to a global LFS object, whose
    address carries their sha256. Concurrent requests share one build, the
    name-only list is kept in ``_manifests``, and the JSON is built off the
    event loop: a big repository's would hold up every other request.
    """
    key = (lakefs_repo, commit, with_metadata)
    if key in _manifests:
        _manifests.move_to_end(key)
        return _manifests[key]
    task = _building.get(key)
    if task is None or task.get_loop() is not asyncio.get_running_loop():
        task = _building[key] = asyncio.ensure_future(_build(repo_row, key))
        task.add_done_callback(lambda t: _building.pop(key, None) if _building.get(key) is t else None)
    return await asyncio.shield(task)


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
