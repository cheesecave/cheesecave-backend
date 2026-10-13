"""``resolve``: revision errors (#58) and conditional requests (#59).

Real LakeFS, real database. What the Hub does, probed read-only against huggingface.co:

* an unknown revision is ``404 RevisionNotFound``, a missing file in a known revision is
  ``404 EntryNotFound``;
* ``refs%2Fheads%2F<name>`` is the branch ``<name>`` on ``resolve``, ``revision`` and ``tree``
  (the client always quotes a revision with ``safe=""``);
* ``If-None-Match`` equal to the ETag answers ``304`` with the validators, for GET and HEAD.
  We also accept what RFC 9110 allows beyond that: ``W/``, a list, ``*``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

REPO = "/models/owner/demo-model"
API = "/api/models/owner/demo-model"


@pytest.fixture
def files_api(backend_test_state):
    """The live ``kohakuhub.api.files`` (the harness reloads modules between sessions)."""
    return backend_test_state.modules.files_module


PLAIN = "README.md"
LFS = "weights/model.safetensors"


# ---------- #58 A: which 404 ----------


@pytest.mark.parametrize("method", ["get", "head"])
async def test_unknown_revision_is_revision_not_found(owner_client, method):
    response = await getattr(owner_client, method)(f"{REPO}/resolve/nope/{PLAIN}")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"
    assert "nope" in response.headers["x-error-message"]


@pytest.mark.parametrize("method", ["get", "head"])
async def test_missing_file_in_known_revision_is_entry_not_found(owner_client, method):
    response = await getattr(owner_client, method)(f"{REPO}/resolve/main/no/such.txt")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "EntryNotFound"
    assert "no/such.txt" in response.headers["x-error-message"]


async def test_unknown_commit_hash_is_revision_not_found(owner_client):
    response = await owner_client.get(f"{REPO}/resolve/{'0' * 40}/{PLAIN}")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("failing", ["get_branch", "stat_object"])
async def test_lakefs_failure_is_a_server_error_not_a_404(
    owner_client, monkeypatch, files_api, failing, method
):
    """A LakeFS outage must not look like a missing file or revision (#10)."""
    real = files_api.get_lakefs_client()

    class Outage:
        def __getattr__(self, name):
            return getattr(real, name)

    async def unavailable(**kwargs):
        request = httpx.Request("GET", "http://lakefs/x")
        raise httpx.HTTPStatusError(
            "unavailable", request=request, response=httpx.Response(503, request=request)
        )

    # Deliberate failure injection: an outage cannot be produced against the real service.
    outage = Outage()
    setattr(outage, failing, unavailable)
    monkeypatch.setattr(files_api, "get_lakefs_client", lambda: outage)
    response = await getattr(owner_client, method)(f"{REPO}/resolve/main/{PLAIN}")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"
    assert "unavailable" in response.headers["x-error-message"]


async def test_a_non_http_failure_reading_the_file_is_a_server_error(
    owner_client, monkeypatch, files_api
):
    real = files_api.get_lakefs_client()

    class Broken:
        def __getattr__(self, name):
            return getattr(real, name)

        async def stat_object(self, **kwargs):
            raise RuntimeError("socket closed")

    monkeypatch.setattr(files_api, "get_lakefs_client", lambda: Broken())
    response = await owner_client.get(f"{REPO}/resolve/main/{PLAIN}")
    assert response.status_code == 500
    assert response.headers["x-error-code"] == "ServerError"


MALFORMED = ["main~1", "main%5E", "main%20x", "a..b", "-x", "main@{1}", "x%00y"]


@pytest.mark.parametrize("revision", MALFORMED)
@pytest.mark.parametrize("method", ["get", "head"])
async def test_a_revision_lakefs_rejects_is_revision_not_found_not_a_server_error(
    owner_client, revision, method
):
    """The Hub: ``Invalid rev id`` is ``404 RevisionNotFound``. A 500 would be retried by the client."""
    response = await getattr(owner_client, method)(f"{REPO}/resolve/{revision}/{PLAIN}")
    assert (response.status_code, response.headers["x-error-code"]) == (404, "RevisionNotFound")


@pytest.mark.parametrize("revision", MALFORMED)
async def test_the_other_routes_say_the_same_of_a_rejected_revision(owner_client, revision):
    for url in (f"{API}/revision/{revision}", f"{API}/tree/{revision}"):
        response = await owner_client.get(url)
        assert (response.status_code, response.headers["x-error-code"]) == (404, "RevisionNotFound"), url


async def test_the_other_revision_routes_agree(owner_client):
    for url in (f"{API}/revision/nope", f"{API}/tree/nope"):
        response = await owner_client.get(url)
        assert response.status_code == 404, url
        assert response.headers["x-error-code"] == "RevisionNotFound", url


# ---------- #58 B: refs/heads/<name> ----------


@pytest.fixture
async def dev_branch(owner_client):
    response = await owner_client.post(f"{API}/branch/dev", json={})
    assert response.status_code == 200, response.text
    yield "dev"
    await owner_client.delete(f"{API}/branch/dev")


@pytest.mark.parametrize("encoded", ["refs%2Fheads%2Fdev", "refs%2fheads%2fdev"])
async def test_encoded_full_ref_names_the_branch(owner_client, dev_branch, encoded):
    plain = await owner_client.get(f"{API}/revision/dev")
    for url in (f"{API}/revision/{encoded}", f"{API}/tree/{encoded}"):
        response = await owner_client.get(url)
        assert response.status_code == 200, url
    assert (await owner_client.get(f"{API}/revision/{encoded}")).json()["sha"] == plain.json()["sha"]

    head = await owner_client.head(f"{REPO}/resolve/{encoded}/{PLAIN}")
    assert head.status_code == 200
    assert head.headers["x-repo-commit"] == plain.json()["sha"]
    assert (await owner_client.get(f"{REPO}/resolve/{encoded}/{PLAIN}")).status_code == 302


async def test_encoded_full_ref_of_a_missing_branch_is_revision_not_found(owner_client):
    response = await owner_client.get(f"{API}/revision/refs%2Fheads%2Fnope")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"
    response = await owner_client.get(f"{REPO}/resolve/refs%2Fheads%2Fnope/{PLAIN}")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"


async def test_a_literal_slashed_ref_is_split_like_the_hub_does(owner_client, dev_branch):
    """The Hub reads ``resolve/refs/heads/dev/x`` as revision ``refs`` and path ``heads/dev/x``."""
    response = await owner_client.get(f"{REPO}/resolve/refs/heads/dev/{PLAIN}")
    assert response.status_code == 404
    assert response.headers["x-error-code"] == "RevisionNotFound"


async def test_an_encoded_slash_elsewhere_is_left_alone(owner_client):
    """Only the revision segment is read as a ref; a file path keeps its meaning."""
    response = await owner_client.get(f"{REPO}/resolve/main/weights%2Fmodel.safetensors")
    assert response.status_code == 302


# ---------- #59: If-None-Match ----------


async def _etag(owner_client, path):
    head = await owner_client.head(f"{REPO}/resolve/main/{path}")
    assert head.status_code == 200
    return head.headers["etag"]


@pytest.mark.parametrize("path", [PLAIN, LFS])
@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize(
    "form",
    ["{e}", '"{e}"', 'W/"{e}"', '"zzz", "{e}"', '"zzz" , W/"{e}"', "*"],
)
async def test_matching_if_none_match_is_304(owner_client, path, method, form):
    etag = (await _etag(owner_client, path)).strip('"')
    plain = await owner_client.head(f"{REPO}/resolve/main/{path}")
    response = await getattr(owner_client, method)(
        f"{REPO}/resolve/main/{path}", headers={"If-None-Match": form.format(e=etag)}
    )
    assert response.status_code == 304
    assert "location" not in response.headers
    assert response.content == b""
    for name in ("etag", "x-repo-commit", "x-linked-etag"):
        assert response.headers[name] == plain.headers[name], name
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", [PLAIN, LFS])
@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("value", ['"deadbeef"', "deadbeef", 'W/"deadbeef"', '"a", "b"', ""])
async def test_other_if_none_match_is_the_normal_answer(owner_client, path, method, value):
    response = await getattr(owner_client, method)(
        f"{REPO}/resolve/main/{path}", headers={"If-None-Match": value}
    )
    assert response.status_code == (302 if method == "get" else 200)


async def test_a_conditional_get_is_not_counted_as_a_download(
    owner_client, monkeypatch, files_api
):
    tracked = []

    async def track(**kwargs):
        tracked.append(kwargs)

    monkeypatch.setattr(files_api, "track_download_async", track)
    etag = await _etag(owner_client, PLAIN)
    assert (
        await owner_client.get(f"{REPO}/resolve/main/{PLAIN}", headers={"If-None-Match": etag})
    ).status_code == 304
    await asyncio.sleep(0.05)
    assert tracked == []
    # the patch is live: a plain GET is counted
    assert (await owner_client.get(f"{REPO}/resolve/main/{PLAIN}")).status_code == 302
    await asyncio.sleep(0.05)
    assert len(tracked) == 1


async def test_if_none_match_does_not_hide_a_missing_file_or_revision(owner_client):
    etag = await _etag(owner_client, PLAIN)
    response = await owner_client.get(
        f"{REPO}/resolve/main/no/such.txt", headers={"If-None-Match": etag}
    )
    assert response.headers["x-error-code"] == "EntryNotFound"
    response = await owner_client.get(
        f"{REPO}/resolve/nope/{PLAIN}", headers={"If-None-Match": "*"}
    )
    assert response.headers["x-error-code"] == "RevisionNotFound"


async def test_an_old_etag_is_not_a_match_after_the_file_changes(owner_client):
    etag = await _etag(owner_client, PLAIN)
    response = await owner_client.post(
        "/api/models/owner/demo-model/commit/main",
        content=(
            '{"key":"header","value":{"summary":"edit"}}\n'
            '{"key":"file","value":{"path":"README.md","content":"IyBjaGFuZ2VkCg==","encoding":"base64"}}\n'
        ),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert response.status_code == 200, response.text
    response = await owner_client.get(f"{REPO}/resolve/main/{PLAIN}", headers={"If-None-Match": etag})
    assert response.status_code == 302
    assert response.headers["etag"] != etag.strip('"')
