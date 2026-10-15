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
import pytest

from kohakuhub import repo_derive, repository_discovery as discovery
from kohakuhub.api.repo.routers.discovery import router
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.db import (
    Commit,
    DailyRepoStats,
    Repository,
    RepositoryCard,
    RepositoryFacet,
    RepositoryMetadata,
    User,
    UserOrganization,
    utcnow,
)
from test.kohakuhub.support import db as db_support

MODELS = [
    User,
    UserOrganization,
    Repository,
    Commit,
    DailyRepoStats,
    RepositoryMetadata,
    RepositoryCard,
    RepositoryFacet,
]


def listing(*paths, more=False):
    """What LakeFS answers for the objects of a commit (one page)."""
    objects = [{"path": p, "path_type": "object", "size_bytes": 1} for p in paths]
    return AsyncMock(return_value={"results": objects, "pagination": {"has_more": more, "next_offset": "x" if more else ""}})


def empty_listing():
    return listing()


@pytest.fixture
def catalog(db_committed, monkeypatch):
    """Endpoint tests run through HTTP on other threads, so rows must be committed."""
    database = db_committed
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
    # Portable upsert: on_conflict_replace() is SQLite-only and fails on Postgres.
    RepositoryMetadata.insert(
        repository=repo,
        main_sha="head",
        metadata=json.dumps({"tags": facets.get("tag", [])}),
        state="ready",
        checked_at=utcnow(),
    ).on_conflict(
        conflict_target=[RepositoryMetadata.repository],
        update={
            RepositoryMetadata.main_sha: "head",
            RepositoryMetadata.metadata: json.dumps({"tags": facets.get("tag", [])}),
            RepositoryMetadata.state: "ready",
            RepositoryMetadata.checked_at: utcnow(),
        },
    ).execute()
    RepositoryFacet.delete().where(RepositoryFacet.repository == repo).execute()
    for key, values in facets.items():
        for value in values:
            RepositoryFacet.create(repository=repo, key=key, value=value)
    # a repository indexed by this release has its card row too (the one that predates it has none)
    RepositoryCard.insert(
        repository=repo, main_sha="head", card="{}", derived="{}", version=repo_derive.VERSION
    ).on_conflict_ignore().execute()


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
        list_objects=empty_listing(),
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
        list_objects=empty_listing(),
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
        list_objects=empty_listing(),
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
    # asyncio.TimeoutError: the builtin TimeoutError only from Python 3.11 on
    with pytest.raises(asyncio.TimeoutError):
        await discovery.read_card_prefix(client, "repo", "sha")
    assert client.get_object_prefix.await_count == 2
    assert deadlines == [0.03] and cancelled


@pytest.mark.asyncio
async def test_changed_sha_and_revoked_lease_cannot_publish_stale_metadata(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = SimpleNamespace(
        list_objects=empty_listing(),
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


# --- the card and what it declares (#62) -------------------------------------


def _readme_client(monkeypatch, readme: bytes | None, head="head-sha"):
    """LakeFS as the one external boundary: a branch head and a README prefix."""
    request = httpx.Request("GET", "https://storage.test/README.md")
    missing = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
    client = SimpleNamespace(
        list_objects=empty_listing(),
        get_branch=AsyncMock(return_value={"commit_id": head}),
        get_object_prefix=AsyncMock(return_value=readme) if readme is not None else AsyncMock(side_effect=missing),
    )
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_indexing_keeps_the_cleaned_card_and_the_fields_it_declares(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _readme_client(monkeypatch, b"---\nlicense: MIT\nlibrary_name: transformers\npipeline_tag: fill-mask\ntags: [exbert]\nlanguage: en\n---\n# Model")

    await discovery.index_repository(repo.id)

    record = RepositoryCard.get_by_id(repo.id)
    assert json.loads(record.card) == {"license": "MIT", "library_name": "transformers", "pipeline_tag": "fill-mask", "tags": ["exbert"], "language": "en"}
    assert json.loads(record.derived) == {
        "tags": ["transformers", "fill-mask", "exbert", "en", "license:mit", "region:us"],
        "pipeline_tag": "fill-mask",
        "library_name": "transformers",
        "sdk": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("readme", [None, b"# a README without a card", b"---\n---\n"])
async def test_a_repository_without_a_card_is_indexed_as_having_none(catalog, monkeypatch, readme):
    repo = repository(catalog, "sample", repo_type="dataset")
    _readme_client(monkeypatch, readme)

    await discovery.index_repository(repo.id)

    record = RepositoryCard.get_by_id(repo.id)
    assert record.card == "{}" and json.loads(record.derived)["tags"] == ["region:us"]


@pytest.mark.asyncio
async def test_a_changed_readme_replaces_the_card_and_a_removed_card_empties_it(catalog, monkeypatch):
    repo = repository(catalog, "sample", repo_type="space")
    client = _readme_client(monkeypatch, b"---\nsdk: gradio\n---\n")
    await discovery.index_repository(repo.id)
    assert json.loads(RepositoryCard.get_by_id(repo.id).derived)["sdk"] == "gradio"

    discovery.mark_dirty(repo.id)
    client.get_object_prefix.return_value = b"# no card any more"
    await discovery.index_repository(repo.id)

    record = RepositoryCard.get_by_id(repo.id)
    assert record.card == "{}" and json.loads(record.derived)["sdk"] is None and RepositoryCard.select().count() == 1


@pytest.mark.asyncio
async def test_a_huge_readme_is_parsed_off_the_event_loop(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _readme_client(monkeypatch, b"---\nlicense: mit\n---\n")
    threads = []
    real = discovery.parse_card_record

    def spy(*args):
        import threading

        threads.append(threading.current_thread() is threading.main_thread())
        return real(*args)

    monkeypatch.setattr(discovery, "parse_card_record", spy)

    await discovery.index_repository(repo.id)

    assert threads == [False]


@pytest.mark.asyncio
async def test_the_info_of_an_indexed_repository_costs_no_storage_read(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _readme_client(monkeypatch, b"---\nlicense: mit\n---\n")
    await discovery.index_repository(repo.id)
    reads = client.get_object_prefix.await_count

    info = await discovery.info_card(repo)

    assert info == {"card": {"license": "mit"}, "derived": {"tags": ["license:mit", "region:us"], "pipeline_tag": None, "library_name": None, "sdk": None}, "info": {}}
    assert client.get_object_prefix.await_count == reads


@pytest.mark.asyncio
async def test_a_repository_not_indexed_yet_is_indexed_when_its_info_is_asked(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _readme_client(monkeypatch, b"---\nlicense: apache-2.0\n---\n")

    info = await discovery.info_card(repo)

    assert info["card"] == {"license": "apache-2.0"} and RepositoryMetadata.get_by_id(repo.id).state == "ready"


@pytest.mark.asyncio
async def test_a_row_indexed_before_the_card_existed_is_read_again(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    index(repo, tag=["old"])
    RepositoryCard.delete().execute()  # ready, with facets, but no card row: indexed before there was one
    client = _readme_client(monkeypatch, b"---\ntags: [fresh]\n---\n")

    info = await discovery.info_card(repo)

    assert info["card"] == {"tags": ["fresh"]} and client.get_object_prefix.await_count == 1


@pytest.mark.asyncio
async def test_an_unreadable_card_is_unknown_not_an_error(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _readme_client(monkeypatch, b"x")
    client.get_branch.side_effect = TimeoutError()

    assert await discovery.info_card(repo) is None


@pytest.mark.asyncio
async def test_a_repository_another_worker_is_indexing_is_unknown_for_now(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _readme_client(monkeypatch, b"---\nlicense: mit\n---\n")
    assert discovery._claim(repo.id) is not None  # a lease held elsewhere

    assert await discovery.info_card(repo) is None


@pytest.mark.asyncio
async def test_a_slow_storage_does_not_hold_the_info_back(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _readme_client(monkeypatch, b"x")

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    client.get_branch.side_effect = slow
    monkeypatch.setattr(discovery, "INFO_INDEX_SECONDS", 0.05)

    assert await discovery.info_card(repo) is None
    assert RepositoryMetadata.get_by_id(repo.id).lease_token is None  # the lease was given back


@pytest.mark.asyncio
async def test_a_card_read_at_an_older_head_is_not_taken_for_the_current_one(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _readme_client(monkeypatch, b"---\nlicense: mit\n---\n", head="old-head")
    await discovery.index_repository(repo.id)
    RepositoryMetadata.update(main_sha="new-head").where(RepositoryMetadata.repository == repo.id).execute()  # published elsewhere
    client.get_branch.return_value = {"commit_id": "new-head"}
    client.get_object_prefix.return_value = b"---\nlicense: apache-2.0\n---\n"

    info = await discovery.info_card(repo)

    assert info["card"] == {"license": "apache-2.0"} and RepositoryCard.get_by_id(repo.id).main_sha == "new-head"


def test_a_repository_removed_takes_its_card_with_it(catalog):
    repo = repository(catalog, "sample")
    RepositoryCard.create(repository=repo, main_sha="h", card="{}", derived="{}")

    Repository.delete().where(Repository.id == repo.id).execute()

    assert RepositoryCard.select().count() == 0


# --- what the files of a model say about it (#63) ----------------------------


def _files_client(monkeypatch, files: dict, readme=b"---\nlicense: mit\n---\n", head="head-sha"):
    """A model repository: ``files`` maps path -> bytes (listed and readable)."""
    request = httpx.Request("GET", "https://storage.test/x")
    missing = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))

    async def prefix(source, ref, path, limit):
        if path == "README.md":
            return readme
        if path in files:
            return files[path][:limit]
        raise missing

    client = SimpleNamespace(
        get_branch=AsyncMock(return_value={"commit_id": head}),
        get_object_prefix=AsyncMock(side_effect=prefix),
        list_objects=listing(*files, "README.md"),
    )
    monkeypatch.setattr(discovery, "get_lakefs_client", lambda: client)
    return client


BERT_FILES = {"config.json": b'{"model_type": "bert", "architectures": ["BertForMaskedLM"], "hidden_size": 8}', "model.safetensors": b"x"}


@pytest.mark.asyncio
async def test_indexing_derives_library_task_tags_and_config_from_the_files(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _files_client(monkeypatch, BERT_FILES)

    await discovery.index_repository(repo.id)

    record = RepositoryCard.get_by_id(repo.id)
    assert json.loads(record.derived) == {
        "tags": ["transformers", "safetensors", "bert", "fill-mask", "license:mit", "region:us"],
        "pipeline_tag": "fill-mask",
        "library_name": "transformers",
        "sdk": None,
    }
    assert json.loads(record.info) == {"config": {"model_type": "bert", "architectures": ["BertForMaskedLM"]}}
    assert record.version == repo_derive.VERSION


@pytest.mark.asyncio
async def test_only_the_files_the_rules_use_are_read(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, {**BERT_FILES, "generation_config.json": b"{}", "tokenizer_config.json": b'{"bos_token": "<s>"}'})

    await discovery.index_repository(repo.id)

    read = sorted(call.args[2] for call in client.get_object_prefix.await_args_list)
    assert read == ["README.md", "config.json", "tokenizer_config.json"]
    assert all(call.args[3] <= discovery.MAX_JSON_BYTES for call in client.get_object_prefix.await_args_list[1:])


@pytest.mark.asyncio
async def test_the_files_are_listed_at_the_head_that_was_read(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES, head="abc123")

    await discovery.index_repository(repo.id)

    assert client.list_objects.await_args.kwargs["ref"] == "abc123"


@pytest.mark.asyncio
@pytest.mark.parametrize("config", ["not-json", "truncated", "not-an-object", "over-the-limit"])
async def test_a_config_that_cannot_be_read_says_nothing(catalog, monkeypatch, config):
    # short ids on purpose: a parameter of a million bytes becomes a test id of a million characters, and the
    # CI log pipeline needed 16 minutes for each line that long
    config = {
        "not-json": b"not json",
        "truncated": b'{"model_type": "bert"',
        "not-an-object": b"[1]",
        "over-the-limit": b"x" * (discovery.MAX_JSON_BYTES + 100),
    }[config]
    repo = repository(catalog, "sample")
    _files_client(monkeypatch, {"config.json": config, "model.safetensors": b"x"})

    await discovery.index_repository(repo.id)

    record = RepositoryCard.get_by_id(repo.id)
    assert json.loads(record.derived)["library_name"] is None and json.loads(record.info) == {}
    assert RepositoryMetadata.get_by_id(repo.id).state == "ready"


@pytest.mark.asyncio
async def test_a_tokenizer_config_too_big_to_read_leaves_the_rest_of_the_config_alone(catalog, monkeypatch):
    """whisper-tiny's is ~800 KB: the Hub lists its special tokens, so an empty entry would be wrong."""
    repo = repository(catalog, "sample")
    _files_client(monkeypatch, {**BERT_FILES, "tokenizer_config.json": b" " * 1100000})

    await discovery.index_repository(repo.id)

    assert json.loads(RepositoryCard.get_by_id(repo.id).info) == {"config": {"model_type": "bert", "architectures": ["BertForMaskedLM"]}}


@pytest.mark.asyncio
async def test_a_listing_in_several_pages_is_read_to_the_end(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    pages = [
        {"results": [{"path": "config.json", "path_type": "object"}, {"path": "sub", "path_type": "common_prefix"}], "pagination": {"has_more": True, "next_offset": "o1"}},
        {"results": [{"path": "model.safetensors", "path_type": "object"}], "pagination": {"has_more": False}},
    ]
    client.list_objects = AsyncMock(side_effect=pages)

    await discovery.index_repository(repo.id)

    assert [c.kwargs["after"] for c in client.list_objects.await_args_list] == ["", "o1"]
    assert json.loads(RepositoryCard.get_by_id(repo.id).derived)["library_name"] == "transformers"


@pytest.mark.asyncio
async def test_a_plain_list_answer_is_one_page(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    client.list_objects = AsyncMock(return_value=[{"path": "config.json", "path_type": "object"}, {"path": "model.safetensors", "path_type": "object"}])

    await discovery.index_repository(repo.id)

    assert json.loads(RepositoryCard.get_by_id(repo.id).derived)["library_name"] == "transformers"


@pytest.mark.asyncio
async def test_a_repository_too_big_to_list_is_indexed_by_its_card_alone(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    monkeypatch.setattr(discovery, "MAX_LISTED_OBJECTS", 1)

    await discovery.index_repository(repo.id)

    record = RepositoryCard.get_by_id(repo.id)
    assert json.loads(record.derived)["library_name"] is None and json.loads(record.info) == {}
    assert record.version == repo_derive.VERSION  # decided, not unknown: it is not listed again at every read
    assert RepositoryMetadata.get_by_id(repo.id).state == "ready"
    assert [c.args[2] for c in client.get_object_prefix.await_args_list] == ["README.md"]


@pytest.mark.asyncio
async def test_a_listing_that_fails_is_a_failed_index_like_an_unreadable_readme(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    client.list_objects = AsyncMock(side_effect=TimeoutError())

    await discovery.index_repository(repo.id)

    record = RepositoryMetadata.get_by_id(repo.id)
    assert record.state == "error" and record.retry_at > utcnow()
    assert RepositoryCard.get_or_none(RepositoryCard.repository == repo.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_type", ["dataset", "space"])
async def test_only_models_are_listed(catalog, monkeypatch, repo_type):
    repo = repository(catalog, "sample", repo_type=repo_type)
    client = _files_client(monkeypatch, BERT_FILES)

    await discovery.index_repository(repo.id)

    assert client.list_objects.await_count == 0
    assert json.loads(RepositoryCard.get_by_id(repo.id).derived)["library_name"] is None


@pytest.mark.asyncio
async def test_what_the_files_say_is_in_the_facets(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _files_client(monkeypatch, BERT_FILES, readme=b"---\ntags: [exbert]\n---\n")

    await discovery.index_repository(repo.id)

    facets = {}
    for row in RepositoryFacet.select().where(RepositoryFacet.repository == repo.id):
        facets.setdefault(row.key, []).append(row.value)
    assert facets["library"] == ["transformers"]
    assert facets["task"] == ["fill-mask"]
    assert sorted(facets["tag"]) == ["bert", "exbert", "safetensors"]


@pytest.mark.asyncio
async def test_a_row_derived_by_older_rules_is_derived_again_when_its_info_is_asked(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    await discovery.index_repository(repo.id)
    RepositoryCard.update(version=0, derived='{"tags": [], "pipeline_tag": null, "library_name": null, "sdk": null}', info="{}").execute()
    reads = client.list_objects.await_count

    info = await discovery.info_card(repo)

    assert client.list_objects.await_count == reads + 1
    assert info["derived"]["library_name"] == "transformers"
    assert info["info"] == {"config": {"model_type": "bert", "architectures": ["BertForMaskedLM"]}}


@pytest.mark.asyncio
async def test_a_row_made_by_the_current_rules_is_not_derived_again(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    await discovery.index_repository(repo.id)
    reads = client.list_objects.await_count

    await discovery.info_card(repo)

    assert client.list_objects.await_count == reads


def test_rows_of_older_rules_are_due_for_another_index_but_stay_in_the_catalog(catalog):
    repo = repository(catalog, "sample")
    index(repo)
    RepositoryCard.update(version=0).execute()
    scope = Repository.select().where(Repository.id == repo.id)

    assert discovery.indexing_progress(scope)["pending"] == 1
    assert RepositoryMetadata.get_by_id(repo.id).state == "ready"

    RepositoryCard.update(version=repo_derive.VERSION).execute()
    assert discovery.indexing_progress(scope)["pending"] == 0


# --- the index follows a commit by itself, and a read waits for it (#63) ------


def test_without_an_event_loop_nothing_is_scheduled(catalog):
    repo = repository(catalog, "sample")

    discovery.schedule_repository(repo.id)  # a thread or a script: the next read or refresh indexes it

    assert discovery._running == {}


@pytest.mark.asyncio
async def test_a_commit_is_indexed_in_the_background(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    _files_client(monkeypatch, BERT_FILES)

    discovery.schedule_repository(repo.id)
    await discovery._running[repo.id]

    assert RepositoryMetadata.get_by_id(repo.id).state == "ready"
    assert json.loads(RepositoryCard.get_by_id(repo.id).derived)["library_name"] == "transformers"
    assert discovery._running == {}


@pytest.mark.asyncio
async def test_commits_during_a_run_cost_one_more_run_not_one_each(catalog, monkeypatch):
    """A run that started before the last commit may have read the old head, so one more run
    follows; ten commits meanwhile are still that one."""
    repo = repository(catalog, "sample")
    runs, gate = [], asyncio.Event()

    async def slow_index(repo_id):
        runs.append(repo_id)
        await gate.wait()

    monkeypatch.setattr(discovery, "index_repository", slow_index)
    discovery.schedule_repository(repo.id)
    await asyncio.sleep(0)
    for _ in range(10):
        discovery.schedule_repository(repo.id)
    gate.set()
    await discovery._running[repo.id]

    assert runs == [repo.id, repo.id]


@pytest.mark.asyncio
async def test_a_failing_run_leaves_nothing_behind(catalog, monkeypatch):
    repo = repository(catalog, "sample")

    async def broken(repo_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(discovery, "index_repository", broken)
    discovery.schedule_repository(repo.id)
    task = discovery._running[repo.id]
    await asyncio.gather(task, return_exceptions=True)

    assert discovery._running == {}
    assert discovery._again == set()


@pytest.mark.asyncio
async def test_a_read_waits_for_the_run_that_is_under_way_instead_of_giving_up(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES)
    discovery.schedule_repository(repo.id)  # holds the lease: indexing it again here would find it taken

    info = await discovery.info_card(repo)

    assert info["derived"]["library_name"] == "transformers"
    assert client.list_objects.await_count == 1  # one index served both


@pytest.mark.asyncio
async def test_a_read_that_runs_out_of_patience_is_unknown_and_leaves_the_run_going(catalog, monkeypatch):
    repo = repository(catalog, "sample")
    gate = asyncio.Event()

    async def slow_index(repo_id):
        await gate.wait()

    monkeypatch.setattr(discovery, "index_repository", slow_index)
    monkeypatch.setattr(discovery, "INFO_INDEX_SECONDS", 0.05)
    discovery.schedule_repository(repo.id)

    assert await discovery.info_card(repo) is None
    assert not discovery._running[repo.id].done()  # the timeout did not cancel the shared run
    gate.set()
    await discovery._running[repo.id]


@pytest.mark.asyncio
async def test_shutdown_cancels_the_runs_in_the_background(catalog, monkeypatch):
    repo = repository(catalog, "sample")

    async def never(repo_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(discovery, "index_repository", never)
    discovery.schedule_repository(repo.id)
    task = discovery._running[repo.id]

    await discovery.close_indexing()

    assert task.cancelled() and discovery._running == {}


@pytest.mark.asyncio
async def test_the_live_head_decides_whether_the_card_is_current(catalog, monkeypatch):
    """The snapshot row can lag the repository (a commit whose invalidation was lost): the info
    route knows the head and says so."""
    repo = repository(catalog, "sample")
    client = _files_client(monkeypatch, BERT_FILES, head="old-head")
    await discovery.index_repository(repo.id)
    reads = client.list_objects.await_count

    assert (await discovery.info_card(repo, head="old-head"))["derived"]["library_name"] == "transformers"
    assert client.list_objects.await_count == reads  # the same head: nothing is read

    client.get_branch.return_value = {"commit_id": "new-head"}
    await discovery.info_card(repo, head="new-head")

    assert client.list_objects.await_count == reads + 1
    assert RepositoryCard.get_by_id(repo.id).main_sha == "new-head"


@pytest.mark.asyncio
async def test_a_read_that_waited_for_a_failing_run_is_unknown_not_an_error(catalog, monkeypatch):
    repo = repository(catalog, "sample")

    async def broken(repo_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(discovery, "index_repository", broken)
    discovery.schedule_repository(repo.id)

    assert await discovery.info_card(repo) is None
