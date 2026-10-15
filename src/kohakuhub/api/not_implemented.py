"""Explicit 501 Not Implemented responses for features KohakuHub does not support.

Why this module exists
----------------------
`huggingface_hub` clients — and, indirectly, every downstream library such as
`gradio` and `datasets` — probe endpoints like ``/discussions`` or
``/spaces/{id}/runtime`` as part of normal flows. If those endpoints do not
exist, FastAPI's default 404 body is generic and the request winds up in
`HfHubHTTPError` without any hint of *why* it failed. That produces confusing
tracebacks and, worse, hides the fact that we intentionally do not support
the feature — a user reading the trace cannot distinguish "KohakuHub bug"
from "KohakuHub scope decision".

This router registers explicit catch-alls for the HuggingFace Hub surfaces
that live in the PR #18 matrix as "Not Supported / Not Claimed":

* Discussions / Pull Requests
* Space runtime management (restart / sleep / hardware / storage / variables / secrets)
* Collections
* Webhooks
* Papers, Jobs, Space logs / templates / hardware list
* Gated-access requests (``user-access-request``; the routes real gating will take over)
* Storage buckets and kernels (see ``early_router`` below)

Each catch-all returns a HuggingFace-compatible 501 response with:

* ``X-Error-Code: NotImplemented`` — read by ``hf_raise_for_status``
* ``X-Error-Message`` — a specific explanation interpolated into the client's
  ``HfHubHTTPError`` message so users can see *which* feature is missing.

``router`` is deliberately registered last in ``main.py`` so it never shadows
real endpoints. ``early_router`` is the one exception: buckets and kernels are not
repository types, but the generic ``/{repo_type}s/...`` routes would otherwise take
their sub-paths first and answer 422 (a type that is not model, dataset or space) or
405, which reads like a bug of ours and not like "not supported". It is registered
before the repository routers and holds static prefixes only (``/buckets``,
``/kernels``); nothing real lives under them. If buckets or kernels are ever
implemented, delete the matching route here.
"""

from fastapi import APIRouter, Request

from kohakuhub.api.repo.utils.hf import hf_not_implemented

router = APIRouter()
early_router = APIRouter()


_DISCUSSION_REASON = (
    "KohakuHub intentionally does not implement the discussions / pull-request "
    "workflow. Use your repo's external issue tracker or contact the maintainer "
    "instead."
)

_SPACE_RUNTIME_REASON = (
    "KohakuHub does not manage Space runtimes, hardware, secrets, or variables "
    "through the hub API. Configure your deployment environment directly."
)

_COLLECTIONS_REASON = "Collections are not implemented by KohakuHub."

_WEBHOOKS_REASON = "Webhooks are not implemented by KohakuHub."

_PAPERS_REASON = "KohakuHub has no paper catalogue."

_JOBS_REASON = "KohakuHub does not run compute for you; run the workload yourself."

_ACCESS_REQUEST_REASON = (
    "A repository here is public or private: there is no gate and no list of "
    "access requests."
)

_BUCKETS_REASON = "Storage buckets are not available. Keep the files in a repository."

_KERNELS_REASON = "Kernel repositories are not available. Use a model repository."

# Every verb a client may send; a refusal does not read the request.
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


# ---------------------------------------------------------------------------
# Discussions / Pull requests
# ---------------------------------------------------------------------------

@router.api_route(
    "/{repo_type}s/{namespace}/{name}/discussions",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def discussions_list_create(
    repo_type: str, namespace: str, name: str, request: Request
):
    return hf_not_implemented("discussions", _DISCUSSION_REASON)


@router.api_route(
    "/{repo_type}s/{namespace}/{name}/discussions/{path:path}",
    methods=["GET", "POST", "DELETE", "PUT"],
    include_in_schema=False,
)
async def discussions_subpath(
    repo_type: str, namespace: str, name: str, path: str, request: Request
):
    return hf_not_implemented("discussions", _DISCUSSION_REASON)


# ---------------------------------------------------------------------------
# Space runtime management
# ---------------------------------------------------------------------------

@router.api_route(
    "/spaces/{namespace}/{name}/{subresource}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
)
async def space_runtime(
    namespace: str, name: str, subresource: str, request: Request
):
    # Only the runtime-management subresources are redirected to 501;
    # anything else falls through (FastAPI matches earlier routes first,
    # so this only fires for unmatched paths under /spaces/*/*).
    runtime_resources = {
        "runtime",
        "restart",
        "sleep",
        "pause",
        "hardware",
        "storage",
        "variables",
        "secrets",
    }
    if subresource in runtime_resources:
        return hf_not_implemented(
            f"Space {subresource}", _SPACE_RUNTIME_REASON
        )
    return hf_not_implemented(
        f"/spaces/{namespace}/{name}/{subresource}",
        _SPACE_RUNTIME_REASON,
    )


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

@router.api_route(
    "/collections",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def collections_root(request: Request):
    return hf_not_implemented("collections", _COLLECTIONS_REASON)


@router.api_route(
    "/collections/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
)
async def collections_subpath(path: str, request: Request):
    return hf_not_implemented("collections", _COLLECTIONS_REASON)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@router.api_route(
    "/settings/webhooks",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def webhooks_root(request: Request):
    return hf_not_implemented("webhooks", _WEBHOOKS_REASON)


@router.api_route(
    "/settings/webhooks/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
)
async def webhooks_subpath(path: str, request: Request):
    return hf_not_implemented("webhooks", _WEBHOOKS_REASON)


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------

@router.api_route("/papers", methods=_ALL_METHODS, include_in_schema=False)
async def papers_root(request: Request):
    return hf_not_implemented("papers", _PAPERS_REASON)


@router.api_route("/papers/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
async def papers_subpath(path: str, request: Request):
    return hf_not_implemented("papers", _PAPERS_REASON)


@router.api_route("/daily_papers", methods=_ALL_METHODS, include_in_schema=False)
async def daily_papers(request: Request):
    return hf_not_implemented("papers", _PAPERS_REASON)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@router.api_route("/jobs", methods=_ALL_METHODS, include_in_schema=False)
async def jobs_root(request: Request):
    return hf_not_implemented("jobs", _JOBS_REASON)


@router.api_route("/jobs/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
async def jobs_subpath(path: str, request: Request):
    return hf_not_implemented("jobs", _JOBS_REASON)


# ---------------------------------------------------------------------------
# Space logs, templates and the hardware list
# (the per-Space runtime routes are ``space_runtime`` above)
# ---------------------------------------------------------------------------

@router.api_route(
    "/spaces/{namespace}/{name}/logs/{log_type}",
    methods=_ALL_METHODS,
    include_in_schema=False,
)
async def space_logs(namespace: str, name: str, log_type: str, request: Request):
    return hf_not_implemented("Space logs", _SPACE_RUNTIME_REASON)


@router.api_route("/spaces/templates", methods=_ALL_METHODS, include_in_schema=False)
async def space_templates(request: Request):
    return hf_not_implemented("Space templates", _SPACE_RUNTIME_REASON)


@router.api_route("/spaces/hardware", methods=_ALL_METHODS, include_in_schema=False)
async def space_hardware_list(request: Request):
    return hf_not_implemented("Space hardware", _SPACE_RUNTIME_REASON)


# ---------------------------------------------------------------------------
# Gated-access requests: ``update_repo_settings(gated=...)`` is refused the same
# way. Real gating (#61) takes these routes over.
# ---------------------------------------------------------------------------

@router.api_route(
    "/{repo_type}s/{namespace}/{name}/user-access-request/{path:path}",
    methods=_ALL_METHODS,
    include_in_schema=False,
)
async def access_requests(
    repo_type: str, namespace: str, name: str, path: str, request: Request
):
    return hf_not_implemented("gated access requests", _ACCESS_REQUEST_REASON)


# ---------------------------------------------------------------------------
# Buckets and kernels: ``early_router``, registered before the repository routers
# ---------------------------------------------------------------------------

@early_router.api_route("/buckets", methods=_ALL_METHODS, include_in_schema=False)
async def buckets_root(request: Request):
    return hf_not_implemented("buckets", _BUCKETS_REASON)


@early_router.api_route("/buckets/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
async def buckets_subpath(path: str, request: Request):
    return hf_not_implemented("buckets", _BUCKETS_REASON)


@early_router.api_route("/kernels", methods=_ALL_METHODS, include_in_schema=False)
async def kernels_root(request: Request):
    return hf_not_implemented("kernels", _KERNELS_REASON)


@early_router.api_route("/kernels/{path:path}", methods=_ALL_METHODS, include_in_schema=False)
async def kernels_subpath(path: str, request: Request):
    return hf_not_implemented("kernels", _KERNELS_REASON)
