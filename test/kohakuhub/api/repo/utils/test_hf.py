"""Tests for HuggingFace compatibility helpers."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import kohakuhub.api.repo.utils.hf as hf_utils
from kohakuhub.db import File
from test.kohakuhub.support.db import table_missing
from test.kohakuhub.support.factories import make_file, make_repo, make_user


def _demo_repo():
    """A real repository row, owner/demo, for the siblings calls."""
    return make_repo(make_user("owner"), "demo")


def test_hf_error_helpers_return_header_only_responses():
    response = hf_utils.hf_error_response(
        418,
        hf_utils.HFErrorCode.BAD_REQUEST,
        "bad tea",
        headers={"X-Trace-Id": "abc"},
    )

    assert response.status_code == 418
    assert response.body == b""
    assert response.headers["x-error-code"] == hf_utils.HFErrorCode.BAD_REQUEST
    assert response.headers["x-error-message"] == "bad tea"
    assert response.headers["x-trace-id"] == "abc"


def test_hf_shortcuts_cover_repo_revision_entry_and_server_errors():
    repo_response = hf_utils.hf_repo_not_found("owner/repo", "dataset")
    gated_response = hf_utils.hf_gated_repo("owner/repo")
    revision_response = hf_utils.hf_revision_not_found("owner/repo", "dev")
    entry_response = hf_utils.hf_entry_not_found("owner/repo", "README.md", "dev")
    bad_request = hf_utils.hf_bad_request("bad input")
    server_error = hf_utils.hf_server_error("boom", error_code="CustomError")

    assert repo_response.headers["x-error-code"] == hf_utils.HFErrorCode.REPO_NOT_FOUND
    assert "dataset" in repo_response.headers["x-error-message"]
    assert gated_response.headers["x-error-code"] == hf_utils.HFErrorCode.GATED_REPO
    assert "accept the terms" in gated_response.headers["x-error-message"]
    assert revision_response.headers["x-error-code"] == hf_utils.HFErrorCode.REVISION_NOT_FOUND
    assert "dev" in revision_response.headers["x-error-message"]
    assert entry_response.headers["x-error-code"] == hf_utils.HFErrorCode.ENTRY_NOT_FOUND
    assert "README.md" in entry_response.headers["x-error-message"]
    assert bad_request.headers["x-error-code"] == hf_utils.HFErrorCode.BAD_REQUEST
    assert server_error.headers["x-error-code"] == "CustomError"


def test_hf_disabled_repo_emits_hf_canonical_message_with_no_x_error_code():
    """``DisabledRepoError`` dispatch in ``hf_raise_for_status`` is keyed
    off the **exact** ``X-Error-Message`` string ``"Access to this resource
    is disabled."`` — no ``X-Error-Code`` is involved. Drift that string
    or add an ``X-Error-Code`` and HF clients fall back to a generic
    ``HfHubHTTPError`` (verified live against ``huggingface_hub`` 1.11.0:
    ``utils/_http.py`` matches the message verbatim before any code-based
    branching). This regression-guards the canonical wire shape so the
    helper is safe to wire up when a future moderation feature lands.
    """
    response = hf_utils.hf_disabled_repo("acme-labs/private-dataset")

    assert response.status_code == 403
    assert response.body == b""
    # Exact HF message string — DisabledRepoError dispatches on it.
    assert (
        response.headers["x-error-message"]
        == "Access to this resource is disabled."
    )
    # No X-Error-Code — HF doesn't set one for DisabledRepo, and adding
    # ours would either be ignored or risk colliding with HF's contract.
    assert "x-error-code" not in response.headers
    # Operator debug aid is fine in our own namespace.
    assert response.headers["x-khub-repo"] == "acme-labs/private-dataset"


def test_hf_disabled_repo_works_without_repo_id():
    """Reserved-for-future-use helper must not require a repo id —
    moderation flows may need to disable a request before any specific
    repo is known."""
    response = hf_utils.hf_disabled_repo()
    assert response.status_code == 403
    assert (
        response.headers["x-error-message"]
        == "Access to this resource is disabled."
    )
    assert "x-khub-repo" not in response.headers


def test_hf_disabled_repo_dispatches_to_disabled_repo_error_in_huggingface_hub():
    """End-to-end: a real ``hf_raise_for_status`` against our wire shape
    must dispatch to ``DisabledRepoError``. This is what proves the
    helper's contract — without this assertion, we're just guessing at
    HF's parsing rules.

    Two skip-worthy gaps in ``huggingface_hub``'s rollout history:

    - ``DisabledRepoError`` itself wasn't exported until ~v0.21; v0.20.3
      (still in the CI matrix) doesn't define the symbol at all.
    - ``hf_raise_for_status`` didn't gain the
      ``X-Error-Message == "Access to this resource is disabled."``
      dispatch branch until ~v1.0; v0.30.x and v0.36.x export the
      ``DisabledRepoError`` class but never raise it from
      ``hf_raise_for_status`` — they fall through to httpx's generic
      ``HTTPStatusError`` instead.

    The combined skip condition is "the round-trip actually produces a
    ``DisabledRepoError``". Probe once at the top of the test and skip
    if the dispatch branch isn't wired in this hf_hub version. The
    helper's on-the-wire shape (status, exact message string, no
    X-Error-Code) is still pinned by the two unit tests above for every
    version.
    """
    import httpx

    try:
        # ``huggingface_hub.errors`` landed around v0.22; older versions
        # keep these exceptions under ``huggingface_hub.utils``. Try the
        # version-portable path, fall back to skip if unavailable.
        from huggingface_hub.utils import DisabledRepoError, HfHubHTTPError
    except ImportError:
        pytest.skip("DisabledRepoError not exported by this hf_hub version")

    from huggingface_hub.utils._http import hf_raise_for_status

    response = hf_utils.hf_disabled_repo("acme-labs/private-dataset")

    def _build_fake() -> httpx.Response:
        # Re-pack our FastAPI response into an httpx.Response so
        # hf_raise_for_status can inspect it the way it would a real
        # wire response from huggingface.co.
        return httpx.Response(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=bytes(response.body),
            request=httpx.Request(
                "GET",
                "https://huggingface.co/api/models/acme-labs/private-dataset",
            ),
        )

    # Probe whether this hf_hub version actually wires the
    # disabled-message dispatch. v0.30 / v0.36 export DisabledRepoError
    # but the dispatch branch in hf_raise_for_status only landed ~v1.0;
    # those older versions raise either ``httpx.HTTPStatusError`` (the
    # raw httpx 4xx default) or a generic ``HfHubHTTPError``. Catch
    # exactly those two families so a future hf_hub version that
    # dispatches the same wire shape to a *different* specific
    # exception (an unlikely but possible API drift) surfaces as a
    # genuine pytest failure instead of being swallowed by an
    # over-broad ``except Exception``.
    try:
        hf_raise_for_status(_build_fake())
    except DisabledRepoError:
        # Already proved the round-trip works; the assertion below would
        # have raised on the next call if we let it.
        return
    except (httpx.HTTPStatusError, HfHubHTTPError):
        pytest.skip(
            "hf_raise_for_status in this version does not dispatch the "
            "X-Error-Message=='Access to this resource is disabled.' "
            "branch to DisabledRepoError"
        )
    pytest.fail("hf_raise_for_status returned cleanly on a 403 disabled response")


def test_hf_error_response_sanitizes_header_values_for_http_transport():
    response = hf_utils.hf_error_response(
        500,
        hf_utils.HFErrorCode.SERVER_ERROR,
        "line 1\nline 2\twith\tspacing",
        headers={"X-Debug": " debug\nvalue "},
    )

    assert response.headers["x-error-message"] == "line 1 line 2 with spacing"
    assert response.headers["x-debug"] == "debug value"


def test_format_hf_datetime_and_lakefs_error_classifiers(monkeypatch):
    seen = {}

    def fake_safe_strftime(value, fmt):
        seen["value"] = value
        seen["fmt"] = fmt
        return "2025-01-15T10:30:45.000000Z"

    monkeypatch.setattr("kohakuhub.utils.datetime_utils.safe_strftime", fake_safe_strftime)

    dt = datetime(2025, 1, 15, 10, 30, 45)

    assert hf_utils.format_hf_datetime(None) is None
    assert hf_utils.format_hf_datetime(dt) == "2025-01-15T10:30:45.000000Z"
    assert seen == {"value": dt, "fmt": "%Y-%m-%dT%H:%M:%S.%fZ"}
    assert hf_utils.is_lakefs_not_found_error(RuntimeError("404 missing")) is True
    assert hf_utils.is_lakefs_not_found_error(RuntimeError("permission denied")) is False
    assert hf_utils.is_lakefs_revision_error(RuntimeError("Unknown branch ref")) is True
    assert hf_utils.is_lakefs_revision_error(RuntimeError("totally different")) is False



LFS_SHA = "c966da3b74697803352ca7c6f2f220e7090a557b619de9da0c6b34d89f7825c1"
LFS_ADDRESS = f"s3://hub/lfs/c9/66/{LFS_SHA}"


def _page(objects, next_offset=None):
    return {
        "results": [
            {"path_type": "object", "path": path, "size_bytes": size, "physical_address": address}
            for path, size, address in objects
        ],
        "pagination": {"has_more": next_offset is not None, "next_offset": next_offset or ""},
    }


class _Lister:
    """LakeFS list_objects over fixed pages, counting calls."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    async def list_objects(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


@pytest.fixture
def lister(monkeypatch):
    hf_utils._manifests.clear()

    def use(*pages):
        client = _Lister(*pages)
        monkeypatch.setattr("kohakuhub.utils.lakefs.get_lakefs_client", lambda: client)
        return client

    yield use
    hf_utils._manifests.clear()


def test_git_blob_id_and_lfs_pointer_match_the_hub():
    """Values the Hub returns for openai-community/gpt2's 64-8bits.tflite."""
    pointer = hf_utils.lfs_pointer(LFS_SHA, 125162496)

    assert len(pointer) == 134
    assert hf_utils.git_blob_id(pointer) == "90e59f00f62c654b1a88a5f127dff14df4611cdc"


async def test_list_repo_objects_pages_and_skips_prefixes(lister):
    client = lister(
        {**_page([("a.txt", 1, "s3://hub/data/x")], "cursor-2"),
         "results": _page([("a.txt", 1, "s3://hub/data/x")])["results"] + [{"path_type": "common_prefix", "path": "d/"}]},
        _page([("b.bin", 2, LFS_ADDRESS)]),
    )

    objects = await hf_utils.list_repo_objects("lake", "c1")

    assert objects == [("a.txt", 1, "s3://hub/data/x"), ("b.bin", 2, LFS_ADDRESS)]
    assert [c["after"] for c in client.calls] == ["", "cursor-2"]
    assert all(c["ref"] == "c1" and c["delimiter"] == "" and c["amount"] == 1000 for c in client.calls)


async def test_list_repo_objects_accepts_a_list_and_a_missing_cursor(lister):
    lister([{"path_type": "object", "path": "a", "size_bytes": 1}])
    assert await hf_utils.list_repo_objects("lake", "c1") == [("a", 1, "")]

    lister({"results": [{"path_type": "object", "path": "a"}], "pagination": {"has_more": True}})
    assert await hf_utils.list_repo_objects("lake", "c1") == [("a", 0, "")]


@pytest.mark.usefixtures("db_scope")
async def test_names_only_siblings_are_built_once_per_commit(lister):
    repo = _demo_repo()
    client = lister(_page([("README.md", 4, "s3://hub/data/r"), ('we"ird.txt', 1, "s3://hub/data/w")]),
                    _page([("README.md", 4, "s3://hub/data/r")]))

    first = await hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=False)
    again = await hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=False)
    other = await hf_utils.hf_siblings_json(repo, "lake", "c2", with_metadata=False)

    assert json.loads(first) == [{"rfilename": "README.md"}, {"rfilename": 'we"ird.txt'}]
    assert again == first
    assert json.loads(other) == [{"rfilename": "README.md"}]
    assert len(client.calls) == 2  # c1 once, c2 once


@pytest.mark.usefixtures("db_scope")
async def test_concurrent_requests_for_one_commit_list_it_once(lister):
    import asyncio

    client = lister(_page([("a", 1, "")]))
    repo = _demo_repo()

    results = await asyncio.gather(
        *(hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=False) for _ in range(4))
    )

    assert len(set(results)) == 1 and len(client.calls) == 1


@pytest.mark.usefixtures("db_scope")
async def test_the_manifest_cache_keeps_to_its_budget(lister, monkeypatch):
    monkeypatch.setattr(hf_utils, "MANIFEST_CACHE_BYTES", 60)
    repo = _demo_repo()
    lister(*[_page([(f"file-{i}.txt", 1, "")]) for i in range(3)])

    for commit in ("c1", "c2", "c3"):
        await hf_utils.hf_siblings_json(repo, "lake", commit, with_metadata=False)

    # Each manifest is 29 bytes: the oldest went
    assert list(hf_utils._manifests) == [("lake", "c2", False), ("lake", "c3", False)]

    monkeypatch.setattr(hf_utils, "MANIFEST_CACHE_BYTES", 10)
    lister(_page([("big-file-name.txt", 1, "")]))
    await hf_utils.hf_siblings_json(repo, "lake", "c4", with_metadata=False)
    assert ("lake", "c4", False) not in hf_utils._manifests  # larger than the whole budget


@pytest.mark.usefixtures("db_scope")
async def test_blob_siblings_follow_the_linked_object(lister):
    """LFS is what LakeFS links (the global lfs/ object), not a size or
    suffix rule; blobIds of regular files are the git blob ids File rows keep."""
    repo = _demo_repo()
    make_file(repo, "README.md", sha256="aa" * 20, size=4)
    make_file(repo, "small.parquet", sha256="bb" * 20, size=10)
    make_file(repo, "model.bin", sha256=LFS_SHA, size=125162496, lfs=True)  # an LFS row gives no blobId
    make_file(repo, "copied.txt", sha256="d41d8cd98f00b204e9800998ecf8427e", size=5)  # a LakeFS checksum, not a git blob id
    lister(_page([
        ("README.md", 4, "s3://hub/data/r"),
        ("small.parquet", 10, "s3://hub/data/p"),  # a suffix rule would call it LFS
        ("model.bin", 125162496, LFS_ADDRESS),
        ("unrecorded.txt", 3, "s3://hub/data/u"),
        ("copied.txt", 5, "s3://hub/data/c"),
    ]))

    siblings = json.loads(await hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=True))

    assert siblings == [
        {"rfilename": "README.md", "blobId": "aa" * 20, "size": 4},
        {"rfilename": "small.parquet", "blobId": "bb" * 20, "size": 10},
        {"rfilename": "model.bin", "blobId": "90e59f00f62c654b1a88a5f127dff14df4611cdc", "size": 125162496,
         "lfs": {"sha256": LFS_SHA, "size": 125162496, "pointerSize": 134}},
        {"rfilename": "unrecorded.txt", "size": 3},
        {"rfilename": "copied.txt", "size": 5},
    ]
    # Not kept: blobIds come from File rows, which a commit records after
    # LakeFS has the commit; a list built in between would keep stale ids
    assert not hf_utils._manifests


@pytest.mark.usefixtures("db_scope")
async def test_concurrent_blob_requests_share_one_build(lister, monkeypatch):
    import asyncio

    client = lister(_page([("model.bin", 9, LFS_ADDRESS)]), _page([("model.bin", 9, LFS_ADDRESS)]))
    loads = []
    real_blob_ids = hf_utils._regular_blob_ids
    # Counts the File reads while they run for real
    monkeypatch.setattr(hf_utils, "_regular_blob_ids", lambda repo: loads.append(1) or real_blob_ids(repo))
    repo = _demo_repo()

    blobs = await asyncio.gather(
        *(hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=True) for _ in range(3))
    )
    names = await hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=False)

    assert len(set(blobs)) == 1 and "blobId" in blobs[0]
    assert json.loads(names) == [{"rfilename": "model.bin"}]
    assert len(client.calls) == 2 and loads == [1]  # one listing for each form
    assert list(hf_utils._manifests) == [("lake", "c1", False)]


@pytest.mark.usefixtures("db_scope")
async def test_blob_siblings_without_file_rows_still_answer(lister, db_scope, monkeypatch):
    repo = make_repo(make_user("a"), "b")
    lister(_page([("README.md", 4, "s3://hub/data/r"), ("model.bin", 9, LFS_ADDRESS)]))

    warnings = []
    monkeypatch.setattr(hf_utils.logger, "warning", warnings.append)

    # The File table cannot be read for real while the siblings are built
    with table_missing(db_scope, File):
        siblings = json.loads(await hf_utils.hf_siblings_json(repo, "lake", "c1", with_metadata=True))

    assert siblings[0] == {"rfilename": "README.md", "size": 4}
    assert siblings[1]["lfs"]["sha256"] == LFS_SHA  # read from the address, not the database
    assert "a/b" in warnings[0]


@pytest.mark.usefixtures("db_scope")
def test_regular_blob_ids_reads_live_regular_rows():
    repo = _demo_repo()
    make_file(repo, "a", sha256="aa")
    make_file(repo, "b", sha256="bb")
    make_file(repo, "deleted.txt", sha256="cc", is_deleted=True)
    make_file(repo, "model.bin", sha256="dd", lfs=True)

    assert hf_utils._regular_blob_ids(repo) == {"a": "aa", "b": "bb"}


@pytest.mark.parametrize("repo_type, prop", [("model", "safetensors"), ("dataset", "citation"), ("space", "sdk")])
def test_expand_accepts_the_hub_properties_of_each_type(repo_type, prop):
    assert hf_utils.expand_error(repo_type, None) is None
    assert hf_utils.expand_error(repo_type, []) is None
    assert hf_utils.expand_error(repo_type, ["sha", prop, "siblings", "storage"]) is None


def test_expand_refuses_an_unknown_property_like_the_hub():
    response = hf_utils.expand_error("model", ["sha", "citation"])

    assert response.status_code == 400
    assert '"citation"' in response.headers["x-error-message"]
    assert '"safetensors"' in response.headers["x-error-message"]  # lists what is accepted


def test_repo_info_response_without_expand_is_every_field_and_the_siblings():
    fields = {"_id": 1, "id": "a/b", "sha": "s", "private": False}

    response = hf_utils.hf_repo_info_response(fields, None, '[{"rfilename": "x"}]')

    assert response.media_type == "application/json"
    assert json.loads(response.body) == {**fields, "siblings": [{"rfilename": "x"}]}


def test_repo_info_response_with_expand_is_only_what_was_asked():
    fields = {"_id": 1, "id": "a/b", "sha": "s", "private": False, "storage": None}

    asked = hf_utils.hf_repo_info_response(fields, ["sha", "sha", "cardData"], None)
    all_time = hf_utils.hf_repo_info_response({**fields, "downloads": 7}, ["downloadsAllTime"], None)
    assert json.loads(all_time.body)["downloadsAllTime"] == 7  # only expanded, as on the Hub
    with_siblings = hf_utils.hf_repo_info_response(fields, ["siblings"], "[]")

    assert json.loads(asked.body) == {"_id": 1, "id": "a/b", "sha": "s", "cardData": None}
    assert json.loads(with_siblings.body) == {"_id": 1, "id": "a/b", "siblings": []}
