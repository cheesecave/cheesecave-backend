"""Contract tests for Hugging Face Hub features KohakuHub explicitly does not
support.

These surfaces appear in the PR #18 compatibility matrix as "Not Supported /
Not Claimed": Discussions / Pull Requests, Space runtime management,
Collections, and Webhooks. Gradio / datasets / transformers can probe these
endpoints during deploy / load flows, so the *shape* of the "no" matters:

* HTTP 501 (not 404 / 500) so the intent is readable on the wire.
* ``X-Error-Code: NotImplemented`` so downstream observability and error
  dashboards can bucket these consistently rather than mixing them with
  real bugs.
* ``X-Error-Message`` with a concrete, actionable explanation — this is
  what ends up inside the ``HfHubHTTPError`` the user sees.
* ``X-Request-Id`` for correlation with backend logs (the middleware applies
  to every response, including these).

Any regression here is a compat-UX regression: the user still gets "your
request failed", but loses the "because KohakuHub intentionally does not
implement feature X" hint.

This module also verifies the *client-visible* half of the contract: when
the user calls `huggingface_hub.HfApi.restart_space(...)` etc. against
KohakuHub, the traceback they get must contain our `X-Error-Message`
verbatim. Without that, the raw HTTP 501 is indistinguishable from a
network glitch.
"""

from __future__ import annotations

import asyncio

import httpx
import huggingface_hub
import pytest
from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError


@pytest.mark.parametrize(
    "method, path, feature_hint",
    [
        # Discussions / PRs
        ("GET", "/api/models/owner/demo-model/discussions", "discussions"),
        ("POST", "/api/models/owner/demo-model/discussions", "discussions"),
        ("GET", "/api/datasets/owner/demo-dataset/discussions/1", "discussions"),
        (
            "POST",
            "/api/models/owner/demo-model/discussions/1/comment",
            "discussions",
        ),
        (
            "POST",
            "/api/models/owner/demo-model/discussions/1/merge",
            "discussions",
        ),
        # Space runtime management
        ("GET", "/api/spaces/owner/demo-space/runtime", "runtime"),
        ("POST", "/api/spaces/owner/demo-space/restart", "restart"),
        ("POST", "/api/spaces/owner/demo-space/sleep", "sleep"),
        ("POST", "/api/spaces/owner/demo-space/hardware", "hardware"),
        ("POST", "/api/spaces/owner/demo-space/secrets", "secrets"),
        ("POST", "/api/spaces/owner/demo-space/variables", "variables"),
        # Collections
        ("GET", "/api/collections", "collections"),
        ("POST", "/api/collections", "collections"),
        ("GET", "/api/collections/some-slug-123", "collections"),
        ("POST", "/api/collections/some-slug-123/item", "collections"),
        # Webhooks
        ("GET", "/api/settings/webhooks", "webhooks"),
        ("POST", "/api/settings/webhooks", "webhooks"),
        ("DELETE", "/api/settings/webhooks/abc", "webhooks"),
        # Papers
        ("GET", "/api/papers/search?q=attention", "papers"),
        ("GET", "/api/papers/1706.03762", "papers"),
        ("GET", "/api/daily_papers", "papers"),
        ("GET", "/api/papers", "papers"),
        # Jobs (the client sends every verb, and the hardware list has no namespace)
        ("GET", "/api/jobs", "jobs"),
        ("GET", "/api/jobs/owner", "jobs"),
        ("POST", "/api/jobs/owner", "jobs"),
        ("GET", "/api/jobs/hardware", "jobs"),
        ("GET", "/api/jobs/owner/abc123", "jobs"),
        ("POST", "/api/jobs/owner/abc123/cancel", "jobs"),
        ("GET", "/api/jobs/owner/abc123/logs", "jobs"),
        # Space logs, templates and the hardware list
        ("GET", "/api/spaces/owner/demo-space/logs/run", "space logs"),
        ("GET", "/api/spaces/owner/demo-space/logs/build", "space logs"),
        ("GET", "/api/spaces/templates", "space templates"),
        ("GET", "/api/spaces/hardware", "space hardware"),
        # A Space sub-resource nobody lists is refused too, and the message names the path
        ("GET", "/api/spaces/owner/demo-space/anything-else", "anything-else"),
        # Gated-access requests (the routes #61 will implement; until then an honest refusal)
        ("GET", "/api/models/owner/demo-model/user-access-request/pending", "access requests"),
        ("GET", "/api/datasets/owner/demo-dataset/user-access-request/accepted", "access requests"),
        ("GET", "/api/spaces/owner/demo-space/user-access-request/rejected", "access requests"),
        ("POST", "/api/models/owner/demo-model/user-access-request/handle", "access requests"),
        ("POST", "/api/models/owner/demo-model/user-access-request/grant", "access requests"),
        # Buckets: sub-paths that the generic /{repo_type}s/... routes used to answer 422 or 405
        ("GET", "/api/buckets", "buckets"),
        ("GET", "/api/buckets/owner", "buckets"),
        ("GET", "/api/buckets/owner/b", "buckets"),
        ("POST", "/api/buckets/owner/b", "buckets"),
        ("DELETE", "/api/buckets/owner/b", "buckets"),
        ("HEAD", "/api/buckets/owner/b", "buckets"),
        ("POST", "/api/buckets/owner/b/batch", "buckets"),
        ("POST", "/api/buckets/owner/b/paths-info", "buckets"),
        ("GET", "/api/buckets/owner/b/settings", "buckets"),
        ("PUT", "/api/buckets/owner/b/settings", "buckets"),
        ("PATCH", "/api/buckets/owner/b/settings", "buckets"),
        ("GET", "/api/buckets/owner/b/tree", "buckets"),
        ("GET", "/api/buckets/owner/b/tree/main/a/b", "buckets"),
        ("GET", "/api/buckets/owner/b/revision/main", "buckets"),
        ("POST", "/api/buckets/owner/b/commit/main", "buckets"),
        ("POST", "/api/buckets/owner/b/preupload/main", "buckets"),
        ("POST", "/api/buckets/owner/b/branch", "buckets"),
        ("POST", "/api/buckets/owner/b/tag", "buckets"),
        # Kernels
        ("GET", "/api/kernels", "kernels"),
        ("GET", "/api/kernels/owner/k", "kernels"),
        ("GET", "/api/kernels/owner/k/revision/main", "kernels"),
        ("GET", "/api/kernels/owner/k/tree/main", "kernels"),
        ("POST", "/api/kernels/owner/k/commit/main", "kernels"),
    ],
)
async def test_unsupported_feature_returns_501_with_hf_headers(
    owner_client, method, path, feature_hint
):
    response = await owner_client.request(method, path)
    assert response.status_code == 501, (
        f"{method} {path} should be 501 Not Implemented, got {response.status_code}: "
        f"{response.text[:200]}"
    )
    assert response.headers.get("x-error-code") == "NotImplemented", (
        f"{method} {path} missing X-Error-Code header; got headers: "
        f"{dict(response.headers)}"
    )
    error_message = response.headers.get("x-error-message", "")
    assert feature_hint.lower() in error_message.lower(), (
        f"{method} {path} X-Error-Message must mention the feature "
        f"({feature_hint!r}); got: {error_message!r}"
    )
    # Middleware always stamps a request id — every response, success or error.
    assert response.headers.get("x-request-id"), (
        f"{method} {path} missing X-Request-Id; expected middleware to stamp it"
    )


async def test_unsupported_route_does_not_shadow_real_endpoints(owner_client):
    """Catch-all 501 routes must not intercept legitimate endpoints.
    ``/api/settings/webhooks`` would be adjacent to ``/api/settings/user`` in
    the path tree; regression here would 501 real settings too."""
    # Real settings endpoint: the user-settings GET — exists and must not 501.
    response = await owner_client.get("/api/settings/user")
    assert response.status_code != 501, (
        "Real /api/settings/user must not be caught by the not-implemented "
        f"catch-all; got {response.status_code}"
    )


async def test_unsupported_feature_error_has_json_safe_header_message(
    owner_client,
):
    """``X-Error-Message`` is a plain ASCII HTTP header — no newlines,
    no tabs. The sanitizer in hf.py collapses whitespace, so regressions
    that bypass the helper would show up as multi-line headers, which
    most HTTP clients reject silently."""
    response = await owner_client.get("/api/models/owner/demo-model/discussions")
    message = response.headers.get("x-error-message", "")
    assert "\n" not in message and "\r" not in message and "\t" not in message
    assert message, "X-Error-Message must not be empty on a 501"


@pytest.mark.parametrize(
    "path, way_out",
    [
        ("/api/papers/1706.03762", "paper catalogue"),
        ("/api/jobs/owner", "compute"),
        ("/api/spaces/owner/demo-space/logs/run", "Space runtimes"),
        ("/api/spaces/templates", "Space runtimes"),
        ("/api/spaces/hardware", "Space runtimes"),
        ("/api/models/owner/demo-model/user-access-request/pending", "public or private"),
        ("/api/buckets/owner/b", "repository"),
        ("/api/kernels/owner/k", "repository"),
    ],
)
async def test_the_refusal_says_what_to_do_instead(owner_client, path, way_out):
    """Not only "no": the message the user reads names the way out."""
    response = await owner_client.get(path)
    assert response.status_code == 501
    message = response.headers.get("x-error-message", "")
    assert way_out.lower() in message.lower(), message
    assert "\n" not in message and "\t" not in message


@pytest.mark.parametrize("kind", ["buckets", "kernels"])
@pytest.mark.parametrize(
    "method, sub",
    [
        ("GET", "/tree/main"),
        ("GET", "/revision/main"),
        ("POST", "/commit/main"),
        ("POST", "/preupload/main"),
        ("POST", "/branch"),
        ("POST", "/tag"),
        ("POST", "/paths-info/main"),
        ("GET", "/settings"),
    ],
)
async def test_buckets_and_kernels_are_refused_before_the_repository_routes(
    owner_client, kind, method, sub
):
    """These prefixes are not repository types, but the generic ``/{repo_type}s/...``
    routes used to take their sub-paths and answer 422 (a type that is not
    model, dataset or space) or 405, which reads like a bug of ours. The refusal
    must come first, so these are 501 and never 422 or 405."""
    response = await owner_client.request(method, f"/api/{kind}/owner/x{sub}")
    assert response.status_code == 501, (response.status_code, response.text[:160])
    assert response.headers.get("x-error-code") == "NotImplemented"


async def test_the_new_refusals_do_not_shadow_real_repository_routes(owner_client):
    """The static prefixes (``/api/buckets``, ``/api/kernels``, ``/api/papers``,
    ``/api/jobs``, ``/api/spaces/templates``, ``/api/spaces/hardware``) must not
    reach into the real endpoints that look alike."""
    for repo_type, name in (("model", "buckets"), ("dataset", "kernels"), ("space", "papers")):
        created = await owner_client.post(
            "/api/repos/create", json={"type": repo_type, "name": name}
        )
        assert created.status_code == 200, created.text
        for route in ("", "/tree/main", "/revision/main", "/commits/main"):
            response = await owner_client.get(f"/api/{repo_type}s/owner/{name}{route}")
            assert response.status_code == 200, (repo_type, name, route, response.status_code)
            assert response.headers.get("x-error-code") != "NotImplemented"
    # A namespace called like a prefix is a namespace: a missing repository, not a refusal.
    for namespace in ("buckets", "kernels", "jobs", "papers"):
        response = await owner_client.get(f"/api/models/{namespace}/nothing-here")
        assert response.status_code == 404, (namespace, response.status_code)
        assert response.headers.get("x-error-code") != "NotImplemented"
    # The real Space runtime routes still answer as before.
    response = await owner_client.get("/api/spaces/owner/papers/runtime")
    assert response.status_code == 501 and "runtime" in response.headers["x-error-message"].lower()


# ---------------------------------------------------------------------------
# Real huggingface_hub-driven checks
# ---------------------------------------------------------------------------
#
# The HTTP-level parametrized block above verifies the server contract.
# These tests verify the *user-visible* half: when someone calls a real
# `HfApi` method that maps to one of these endpoints, the `HfHubHTTPError`
# they see in their traceback must contain our X-Error-Message verbatim.
# If the text is swallowed, the user is left with a bare 501 and no clue
# why — which is exactly the UX we are trying to avoid.


def _assert_server_message_mentions(
    exc: HfHubHTTPError, feature_hint: str
) -> None:
    """Pin both `HfHubHTTPError.server_message` (read from X-Error-Message
    by the huggingface_hub client) and the final `str(exc)` traceback.

    `server_message` is the canonical place to check — huggingface_hub
    surfaces it on the exception instance. `str(exc)` is the secondary
    check because it's literally what prints in tracebacks; if that
    doesn't mention the feature, the end-user sees a useless error.
    """
    server_msg = (exc.server_message or "").lower()
    traceback_msg = str(exc).lower()
    hint = feature_hint.lower()
    assert hint in server_msg or hint in traceback_msg, (
        f"HfHubHTTPError must mention {feature_hint!r} in either "
        f"server_message or traceback text; got server_message="
        f"{exc.server_message!r}, str={str(exc)[:300]!r}"
    )
    assert "cheesecave" in server_msg or "cheesecave" in traceback_msg, (
        "The user-visible error must identify CheeseCave as the reason for "
        "the 'not implemented' — otherwise users cannot tell whether this "
        "is a hub-side refusal or a library bug. Got: "
        f"server_message={exc.server_message!r}"
    )


@pytest.mark.hf_client
async def test_hf_api_get_repo_discussions_raises_readable_not_implemented(
    live_server_url, hf_api_token
):
    """`HfApi.get_repo_discussions` is called by gradio / community-tab
    UIs. Users running against KohakuHub need to see a clear "discussions
    is not supported" message — not a generic 501 traceback."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    with pytest.raises(HfHubHTTPError) as excinfo:
        await asyncio.to_thread(
            lambda: list(api.get_repo_discussions(repo_id="owner/demo-model"))
        )
    _assert_server_message_mentions(excinfo.value, "discussions")


@pytest.mark.hf_client
async def test_hf_api_create_discussion_raises_readable_not_implemented(
    live_server_url, hf_api_token
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    with pytest.raises(HfHubHTTPError) as excinfo:
        await asyncio.to_thread(
            api.create_discussion,
            repo_id="owner/demo-model",
            title="some discussion",
            description="body",
        )
    _assert_server_message_mentions(excinfo.value, "discussions")


@pytest.mark.hf_client
async def test_hf_api_restart_space_raises_readable_not_implemented(
    live_server_url, hf_api_token
):
    """`HfApi.restart_space` is called by Space lifecycle automation. The
    user-facing error must say "Space restart is not supported" so their
    deploy scripts can fail loudly and clearly instead of retrying
    against a generic 501."""
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    # No need to actually create a Space — the 501 catch-all fires before
    # any lookup, and that's the whole point.
    with pytest.raises(HfHubHTTPError) as excinfo:
        await asyncio.to_thread(api.restart_space, "owner/demo-space")
    _assert_server_message_mentions(excinfo.value, "restart")


@pytest.mark.hf_client
async def test_hf_api_pause_space_raises_readable_not_implemented(
    live_server_url, hf_api_token
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    with pytest.raises(HfHubHTTPError) as excinfo:
        await asyncio.to_thread(api.pause_space, "owner/demo-space")
    # hf_hub calls POST /spaces/.../pause — our catch-all fires with the
    # generic space-runtime reason, which still names "Space" explicitly.
    _assert_server_message_mentions(excinfo.value, "space")


@pytest.mark.hf_client
async def test_hf_api_add_space_secret_raises_readable_not_implemented(
    live_server_url, hf_api_token
):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    with pytest.raises(HfHubHTTPError) as excinfo:
        await asyncio.to_thread(
            api.add_space_secret, "owner/demo-space", "MY_SECRET", "value"
        )
    _assert_server_message_mentions(excinfo.value, "secrets")


async def test_direct_webhooks_http_surface_returns_not_implemented(
    live_server_url, hf_api_token
):
    """`HfApi.list_webhooks` in huggingface_hub ignores the caller-supplied
    ``endpoint`` and hard-codes ``constants.ENDPOINT`` (huggingface.co),
    so it never actually reaches KohakuHub. The direct-HTTP contract
    below is the only meaningful pin for the webhooks-not-implemented
    surface at the KohakuHub boundary.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{live_server_url}/api/settings/webhooks",
            headers={"Authorization": f"Bearer {hf_api_token}"},
        )
    assert response.status_code == 501
    assert response.headers.get("x-error-code") == "NotImplemented"
    message = response.headers.get("x-error-message", "")
    assert "webhook" in message.lower()
    assert "cheesecave" in message.lower()


async def test_direct_discussions_http_surface_surfaces_x_error_fields(
    live_server_url, hf_api_token
):
    """Some library code paths (and humans with curl) hit these endpoints
    directly via httpx rather than through `HfApi`. Pin that the raw
    headers carry both X-Error-Code and X-Error-Message on the 501 so
    that error dashboards, log ingesters, and client retry logic can
    distinguish "we don't implement this" from "transient backend
    failure"."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{live_server_url}/api/models/owner/demo-model/discussions",
            headers={"Authorization": f"Bearer {hf_api_token}"},
        )
    assert response.status_code == 501
    assert response.headers.get("x-error-code") == "NotImplemented"
    message = response.headers.get("x-error-message", "")
    assert "discussion" in message.lower()
    assert "cheesecave" in message.lower()
    # Middleware applies to live_server_url responses too.
    assert response.headers.get("x-request-id"), (
        "Live-server responses must also carry X-Request-Id"
    )



# ---------------------------------------------------------------------------
# The refused features through the real client
# ---------------------------------------------------------------------------
#
# One real ``HfApi`` call per refused surface, for whichever huggingface_hub is
# installed (the CI matrix runs 0.20.3 to the newest): a method the version does
# not have is skipped. 2.x raises ``HfHubHTTPError`` whose text carries our message.
# Three older releases build ``list_jobs`` on a bare ``raise_for_status``: there the
# status is all the user gets, so the test pins the 501 and not the text.


def _refused(api, name, call):
    if not hasattr(api, name):
        pytest.skip(f"this huggingface_hub has no HfApi.{name}")
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the type differs by client version
        return exc
    raise AssertionError(f"HfApi.{name} did not raise: the server refused it with 501")


@pytest.mark.hf_client
@pytest.mark.parametrize(
    "name, hint, call",
    [
        ("paper_info", "papers", lambda a: a.paper_info("1706.03762")),
        ("list_papers", "papers", lambda a: list(a.list_papers(query="attention"))),
        ("list_daily_papers", "papers", lambda a: list(a.list_daily_papers())),
        ("list_jobs", "jobs", lambda a: list(a.list_jobs(namespace="owner"))),
        ("list_jobs_hardware", "jobs", lambda a: a.list_jobs_hardware()),
        ("create_bucket", "buckets", lambda a: a.create_bucket("owner/some-bucket")),
        ("list_buckets", "buckets", lambda a: list(a.list_buckets(namespace="owner"))),
        ("kernel_info", "kernels", lambda a: a.kernel_info("owner/some-kernel")),
        ("list_space_templates", "space templates", lambda a: a.list_space_templates()),
        ("list_spaces_hardware", "space hardware", lambda a: a.list_spaces_hardware()),
        ("fetch_space_logs", "space logs", lambda a: list(a.fetch_space_logs("owner/demo-space", follow=False))),
    ],
)
async def test_hf_api_refused_surfaces_raise_a_readable_501(live_server_url, hf_api_token, name, hint, call):
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    exc = await asyncio.to_thread(_refused, api, name, lambda: call(api))
    assert str(exc.response.url).startswith(live_server_url), (
        f"the request must reach the test server, not the public Hub: {exc.response.url}"
    )
    assert exc.response.status_code == 501, repr(exc)
    if isinstance(exc, HfHubHTTPError):
        _assert_server_message_mentions(exc, hint)
    else:
        assert name == "list_jobs", f"{name} raised {type(exc).__name__} without our message: {exc}"


@pytest.mark.hf_client
@pytest.mark.parametrize(
    "name, call",
    [
        ("list_pending_access_requests", lambda a: list(a.list_pending_access_requests("owner/demo-model"))),
        ("list_accepted_access_requests", lambda a: list(a.list_accepted_access_requests("owner/demo-model"))),
        ("list_rejected_access_requests", lambda a: list(a.list_rejected_access_requests("owner/demo-model"))),
        ("grant_access", lambda a: a.grant_access("owner/demo-model", "bob")),
        ("accept_access_request", lambda a: a.accept_access_request("owner/demo-model", "bob")),
    ],
)
async def test_hf_api_access_requests_raise_a_readable_501(
    live_server_url, hf_api_token, monkeypatch, name, call
):
    """These methods write ``constants.ENDPOINT`` into the URL (they ignore the
    endpoint the ``HfApi`` was given), so the test points the constant at the server."""
    monkeypatch.setattr(huggingface_hub.constants, "ENDPOINT", live_server_url)
    # Older releases (0.20) import ENDPOINT into hf_api by name: patch that copy too, or the
    # request would go to huggingface.co.
    monkeypatch.setattr(huggingface_hub.hf_api, "ENDPOINT", live_server_url, raising=False)
    api = HfApi(endpoint=live_server_url, token=hf_api_token)
    exc = await asyncio.to_thread(_refused, api, name, lambda: call(api))
    assert str(exc.response.url).startswith(live_server_url), (
        f"the request must reach the test server, not the public Hub: {exc.response.url}"
    )
    assert isinstance(exc, HfHubHTTPError) and exc.response.status_code == 501, repr(exc)
    _assert_server_message_mentions(exc, "access requests")
