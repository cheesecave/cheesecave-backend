"""Discovery checks cover the full catalog, private facets and asynchronous publish races."""

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from peewee import SqliteDatabase
import pytest

from kohakuhub import repository_discovery as discovery
from kohakuhub.api.repo.routers.discovery import router
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.db import (
    Commit,
    DailyRepoStats,
    Repository,
    RepositoryFacet,
    RepositoryMetadata,
    User,
    UserOrganization,
    utcnow,
)

MODELS = [
    User,
    UserOrganization,
    Repository,
    Commit,
    DailyRepoStats,
    RepositoryMetadata,
    RepositoryFacet,
]


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    database = SqliteDatabase(str(tmp_path / "catalog.db"), pragmas={"foreign_keys": 1})
    original = {model: model._meta.database for model in MODELS}
    database.bind(MODELS)
    database.create_tables(MODELS)
    owner = User.create(username="owner", normalized_name="owner", email="owner@example.com")
    outsider = User.create(
        username="outsider", normalized_name="outsider", email="outsider@example.com"
    )
    org = User.create(username="org", normalized_name="org", is_org=True)
    UserOrganization.create(user=owner, organization=org, role="member")
    auth = {"user": None}
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_optional_user] = lambda: auth["user"]
    real_schedule = discovery.schedule_indexing
    monkeypatch.setattr(discovery, "schedule_indexing", lambda scope: None)
    try:
        with TestClient(app) as session:
            yield SimpleNamespace(
                database=database,
                owner=owner,
                outsider=outsider,
                org=org,
                auth=auth,
                session=session,
                real_schedule=real_schedule,
            )
    finally:
        database.close()
        for model, connection in original.items():
            model.bind(connection)


def repository(catalog, name, repo_type="model", private=False, namespace="owner", **values):
    return Repository.create(
        repo_type=repo_type,
        namespace=namespace,
        name=name,
        full_id=f"{namespace}/{name}",
        owner=catalog.org if namespace == "org" else catalog.owner,
        private=private,
        created_at=datetime(2025, 1, 1),
        **values,
    )


def index(repo, **facets):
    RepositoryMetadata.insert(
        repository=repo,
        main_sha="head",
        metadata=json.dumps({"tags": facets.get("tag", [])}),
        state="ready",
        checked_at=utcnow(),
    ).on_conflict_replace().execute()
    RepositoryFacet.delete().where(RepositoryFacet.repository == repo).execute()
    for key, values in facets.items():
        for value in values:
            RepositoryFacet.create(repository=repo, key=key, value=value)


def read(catalog, suffix="", kind="models"):
    response = catalog.session.get(f"/api/{kind}/discover{suffix}")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def options(result, key):
    return {
        option["value"]: option["count"]
        for facet in result["facets"]
        if facet["key"] == key
        for option in facet["options"]
    }


def test_full_catalog_filtering_and_stable_pagination_beyond_one_hundred(catalog):
    for n in range(125):
        repo = repository(catalog, f"model-{n:03}")
        index(repo, tag=["late" if n == 124 else "common"], license=["mit"])
    result = read(catalog, "?tag=late")
    assert result["total"] == 1
    assert result["items"][0]["id"] == "owner/model-124"
    result = read(catalog, "?limit=10&offset=110&sort=recent")
    assert result["total"] == 125 and result["has_more"]
    assert [row["id"] for row in result["items"]] == [
        f"owner/model-{n:03}" for n in range(14, 4, -1)
    ]
    assert read(catalog, "?limit=10&offset=110&sort=recent")["items"] == result["items"]


def test_or_within_and_across_dimensions_and_disjunctive_counts(catalog):
    for name, tag, license in [("a", "one", "mit"), ("b", "two", "apache"), ("c", "two", "mit")]:
        index(repository(catalog, name), tag=[tag], license=[license])
    result = read(catalog, "?tag=one&tag=two&license=mit")
    assert result["total"] == 2
    assert options(result, "tag") == {"one": 1, "two": 1}
    assert options(result, "license") == {"mit": 2, "apache": 1}
    absent = read(catalog, "?tag=does-not-exist")
    assert absent["total"] == 0 and absent["items"] == []
    assert options(absent, "tag")["does-not-exist"] == 0
    assert read(catalog, "?search=unmatched")["total"] == 0


def test_private_and_org_facets_counts_and_pending_do_not_leak(catalog):
    index(repository(catalog, "public"), tag=["public"])
    index(repository(catalog, "own", private=True), tag=["own-secret"])
    index(repository(catalog, "org", private=True, namespace="org"), tag=["org-secret"])
    repository(catalog, "pending-private", private=True, namespace="org")
    anonymous = read(catalog)
    assert anonymous["total"] == 1 and options(anonymous, "tag") == {"public": 1}
    assert anonymous["indexing"] == {"pending": 0, "total": 1}
    catalog.auth["user"] = catalog.outsider
    assert read(catalog)["total"] == 1
    catalog.auth["user"] = catalog.owner
    member = read(catalog)
    assert member["total"] == 4
    assert member["indexing"] == {"pending": 1, "total": 4}
    assert options(member, "tag") == {"org-secret": 1, "own-secret": 1, "public": 1}


def test_selected_filters_return_authoritative_unicode_casefold_and_dedup(catalog):
    index(repository(catalog, "sample"), tag=["strasse", "σ", "ffi"])
    response = catalog.session.get(
        "/api/models/discover",
        params=[("tag", value) for value in [" Straße ", "STRASSE", "ς", "Σ", "ﬃ", "ffi"]],
    )
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 1
    assert result["selected"] == {
        key: ["strasse", "σ", "ffi"] if key == "tag" else [] for key in discovery.FACETS
    }
    assert set(result["selected"]["tag"]) == set(options(result, "tag"))


@pytest.mark.parametrize(
    "kind,repo_type,facet",
    [("models", "model", "task"), ("datasets", "dataset", "size"), ("spaces", "space", "sdk")],
)
def test_all_repository_types_and_all_declared_facets(catalog, kind, repo_type, facet):
    repo = repository(catalog, "sample", repo_type=repo_type)
    index(repo, **{facet: ["value"]})
    assert read(catalog, f"?{facet}=value", kind)["total"] == 1


def test_trending_filters_before_paging_and_hides_higher_ranked_private_rows(catalog, monkeypatch):
    low = repository(catalog, "low")
    high = repository(catalog, "high", private=True)
    match = repository(catalog, "match")
    index(low, tag=["other"])
    index(high, tag=["wanted"])
    index(match, tag=["wanted"])
    monkeypatch.setattr(
        "kohakuhub.api.utils.trending.calculate_trending_scores",
        lambda rt: {high.id: 99, low.id: 9, match.id: 1},
    )
    assert read(catalog, "?tag=wanted&limit=1")["items"][0]["id"] == match.full_id
    catalog.auth["user"] = catalog.owner
    assert read(catalog, "?tag=wanted&limit=1")["items"][0]["id"] == high.full_id


def test_invalid_filters_do_not_fall_back_to_all_rows(catalog):
    repository(catalog, "sample")
    for suffix in ("?tag=", "?limit=101", "?offset=-1", "?sort=invalid", "?tag=%00"):
        assert catalog.session.get("/api/models/discover" + suffix).status_code == 422


def test_main_invalidation_removes_stale_facets_immediately(catalog):
    repo = repository(catalog, "sample")
    index(repo, tag=["old"])
    discovery.mark_dirty(repo.id)
    assert read(catalog, "?tag=old")["total"] == 0
    assert read(catalog)["indexing"]["pending"] == 1
    assert RepositoryMetadata.get_by_id(repo.id).generation == 1


def test_parse_real_declared_metadata_and_bounded_yaml():
    card = b"---\nlicense: mit\nlanguage: [en, no]\npipeline_tag: image-to-text\nlibrary_name: transformers\ntags: [vision, 'format:parquet', 'modality:image']\nsize_categories: 1K<n<10K\nsdk: gradio\n---\nbody"
    metadata, facets = discovery.parse_card(card)
    assert metadata["language"] == ["en", "no"]
    assert facets["task"] == ["image-to-text"] and facets["format"] == ["parquet"]
    assert facets["modality"] == ["image"] and facets["sdk"] == ["gradio"]
    for content in (
        b"# Plain README",
        b"---\ntags: [broken\n---",
        b"---\nx: &x [a]\ntags: *x\n---",
        b"---\n- not-a-mapping\n---",
        b"---\ntags: [missing-end]",
    ):
        assert all(not values for values in discovery.parse_card(content)[1].values())


def test_shared_frontmatter_fixtures_match_normalized_index_metadata():
    path = Path(__file__).resolve().parents[1] / "fixtures/repository_card_frontmatter.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert cases
    recognized = {name for names in discovery.FIELDS.values() for name in names} | {
        "base_model",
        "datasets",
        "license_name",
    }
    for case in cases:
        expected = {
            name: value if isinstance(value, list) else [value]
            for name, value in case["metadata"].items()
            if name in recognized
        }
        actual, _ = discovery.parse_card(case["source"].encode("utf-8"))
        assert actual == expected, case["name"]


@pytest.mark.asyncio
async def test_indexing_uses_immutable_sha_and_replaces_removed_facets(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    index(repo, tag=["removed"])
    discovery.mark_dirty(repo.id)
    client = SimpleNamespace(
        get_branch=AsyncMock(return_value={"commit_id": "new-sha"}),
        get_object_prefix=AsyncMock(return_value=b"---\ntags: [new]\n---\n"),
    )
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    await discovery.index_repository(repo.id)
    assert RepositoryMetadata.get_by_id(repo.id).main_sha == "new-sha"
    assert (
        list(RepositoryFacet.select().where(RepositoryFacet.repository == repo).tuples())[0][-1]
        == "new"
    )
    assert client.get_object_prefix.await_args.args[1] == "new-sha"


@pytest.mark.asyncio
async def test_missing_readme_is_indexed_empty_and_transient_errors_back_off(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    request = httpx.Request("GET", "https://storage.test/README.md")
    error = httpx.HTTPStatusError(
        "missing", request=request, response=httpx.Response(404, request=request)
    )
    client = SimpleNamespace(
        get_branch=AsyncMock(return_value={"commit_id": "head"}),
        get_object_prefix=AsyncMock(side_effect=error),
    )
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    await discovery.index_repository(repo.id)
    assert RepositoryMetadata.get_by_id(repo.id).state == "ready"
    assert [call.args[2] for call in client.get_object_prefix.await_args_list] == list(
        discovery.README_CANDIDATES
    )
    discovery.mark_dirty(repo.id)
    client.get_branch.side_effect = TimeoutError()
    await discovery.index_repository(repo.id)
    record = RepositoryMetadata.get_by_id(repo.id)
    assert record.state == "error" and record.retry_at > utcnow()
    calls = client.get_branch.await_count
    await discovery.index_repository(repo.id)
    assert client.get_branch.await_count == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["README.md", "readme.md", "Readme.md"])
async def test_readme_candidates_preserve_order_bom_and_eof(catalog, monkeypatch, filename):
    repo = repository(catalog, "sample")

    async def prefix(source, head, path, max_bytes):
        assert head == "immutable" and max_bytes == discovery.MAX_README_BYTES
        if path == filename:
            return b"\xef\xbb\xbf---\nlicense: mit\ntags: [case-sensitive]\n---"
        request = httpx.Request("GET", "https://storage.test/" + path)
        raise httpx.HTTPStatusError(
            "missing", request=request, response=httpx.Response(404, request=request)
        )

    client = SimpleNamespace(
        get_branch=AsyncMock(return_value={"commit_id": "immutable"}),
        get_object_prefix=AsyncMock(side_effect=prefix),
    )
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    await discovery.index_repository(repo.id)
    assert read(catalog, "?tag=case-sensitive")["total"] == 1
    assert [call.args[2] for call in client.get_object_prefix.await_args_list] == list(
        discovery.README_CANDIDATES[: discovery.README_CANDIDATES.index(filename) + 1]
    )


@pytest.mark.asyncio
async def test_readme_candidates_share_deadline_and_do_not_hide_storage_errors(monkeypatch):
    request = httpx.Request("GET", "https://storage.test/README.md")
    failure = httpx.HTTPStatusError(
        "offline", request=request, response=httpx.Response(503, request=request)
    )
    client = SimpleNamespace(get_object_prefix=AsyncMock(side_effect=failure))
    with pytest.raises(httpx.HTTPStatusError):
        await discovery.read_card_prefix(client, "repo", "sha")
    assert client.get_object_prefix.await_count == 1

    cancelled = False

    async def slow_missing(*args):
        nonlocal cancelled
        if args[2] == "README.md":
            raise httpx.HTTPStatusError(
                "missing", request=request, response=httpx.Response(404, request=request)
            )
        try:
            await asyncio.Future()
        finally:
            cancelled = True

    client.get_object_prefix = AsyncMock(side_effect=slow_missing)
    wait_for = asyncio.wait_for
    deadlines = []

    async def record_deadline(awaitable, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, timeout)

    monkeypatch.setattr(discovery, "README_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(discovery.asyncio, "wait_for", record_deadline)
    with pytest.raises(TimeoutError):
        await discovery.read_card_prefix(client, "repo", "sha")
    assert client.get_object_prefix.await_count == 2
    assert deadlines == [0.03] and cancelled


@pytest.mark.asyncio
async def test_changed_sha_and_revoked_lease_cannot_publish_stale_metadata(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = SimpleNamespace(
        get_branch=AsyncMock(side_effect=[{"commit_id": "old"}, {"commit_id": "new"}]),
        get_object_prefix=AsyncMock(return_value=b"---\ntags: [old]\n---\n"),
    )
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    await discovery.index_repository(repo.id)
    assert RepositoryMetadata.get_by_id(repo.id).state == "pending"
    assert RepositoryFacet.select().count() == 0

    async def revoke(*args):
        discovery.mark_dirty(repo.id)
        return b"---\ntags: [stale]\n---\n"

    client.get_branch = AsyncMock(return_value={"commit_id": "new"})
    client.get_object_prefix = revoke
    await discovery.index_repository(repo.id)
    assert RepositoryFacet.select().count() == 0


def test_lease_prevents_duplicate_claim_and_expiration_can_recover(catalog):
    repo = repository(catalog, "sample")
    first = discovery._claim(repo.id)
    assert first is not None and discovery._claim(repo.id) is None
    RepositoryMetadata.update(lease_until=utcnow() - timedelta(seconds=1)).where(
        RepositoryMetadata.repository == repo
    ).execute()
    assert discovery._claim(repo.id)[0] != first[0]


@pytest.mark.asyncio
async def test_ttl_refresh_preserves_ready_facets_during_transient_failure(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    index(repo, tag=["cached"])
    RepositoryMetadata.update(checked_at=utcnow() - timedelta(minutes=6)).where(
        RepositoryMetadata.repository == repo
    ).execute()
    client = SimpleNamespace(get_branch=AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    await discovery.index_repository(repo.id)
    assert RepositoryMetadata.get_by_id(repo.id).state == "ready"
    assert read(catalog, "?tag=cached")["total"] == 1
    assert read(catalog)["indexing"]["pending"] == 1
    discovery.mark_dirty(repo.id)
    await discovery.index_repository(repo.id)
    assert read(catalog, "?tag=cached")["total"] == 0


def test_last_modified_comes_from_batched_main_commit_records(catalog):
    repo = repository(catalog, "sample")
    index(repo, tag=["cached"])
    at = datetime(2026, 1, 2)
    Commit.create(
        repository=repo,
        owner=catalog.owner,
        author=catalog.owner,
        username="owner",
        repo_type="model",
        branch="main",
        commit_id="latest",
        message="Update",
        created_at=at,
    )
    assert read(catalog)["items"][0]["lastModified"] == at.isoformat()


def test_common_commit_recording_invalidates_only_main(catalog):
    from kohakuhub.db_operations import create_commit

    repo = repository(catalog, "sample")
    index(repo, tag=["cached"])
    for branch in ("dev", "main"):
        create_commit(
            commit_id=branch,
            repository=repo,
            repo_type="model",
            branch=branch,
            author=catalog.owner,
            username="owner",
            message="Update",
        )
        assert RepositoryMetadata.get_by_id(repo.id).state == (
            "ready" if branch == "dev" else "pending"
        )


@pytest.mark.asyncio
async def test_background_batch_returns_immediately_and_bounds_work(catalog, monkeypatch):
    for n in range(45):
        repository(catalog, f"sample-{n}")
    active = 0
    maximum = 0
    processed = []

    async def fake_index(repo_id):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        processed.append(repo_id)
        active -= 1

    monkeypatch.setattr(discovery, "index_repository", fake_index)
    catalog.real_schedule(Repository.select())
    assert processed == []
    await asyncio.gather(*list(discovery._batches))
    assert len(processed) == 40 and maximum <= 4
    await discovery.close_indexing()


@pytest.mark.asyncio
async def test_prefix_download_is_bounded_when_storage_ignores_range():
    from kohakuhub.lakefs_rest_client import LakeFSRestClient

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * 4096
            raise AssertionError("Indexer read past its prefix")

        async def aclose(self):
            self.closed = True

    body = Body()

    def respond(request):
        assert request.headers["Range"] == "bytes=0-127"
        return httpx.Response(200, stream=body)

    client = LakeFSRestClient("http://storage.test", "test", "test")
    client._httpx_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await client.get_object_prefix("repo", "sha", "README.md", 128) == b"x" * 128
        assert body.closed
    finally:
        await client.aclose()
