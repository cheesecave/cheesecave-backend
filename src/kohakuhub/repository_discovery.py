"""SQL discovery facets and bounded, lease-fenced README indexing in the API process."""

import asyncio
from datetime import timedelta
import json
import re
from uuid import uuid4

import httpx
from peewee import JOIN, fn
import yaml

from kohakuhub.db import Commit, Repository, RepositoryFacet, RepositoryMetadata, utcnow
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

logger = get_logger("DISCOVERY")
M = RepositoryMetadata
F = RepositoryFacet
MAX_README_BYTES = 65536
README_CANDIDATES = ("README.md", "readme.md", "Readme.md")
README_TIMEOUT_SECONDS = 3
REFRESH_AFTER = timedelta(minutes=5)
RETRY_AFTER = timedelta(seconds=30)
LEASE_AFTER = timedelta(seconds=60)
FACETS = {
    "task": "Tasks",
    "library": "Libraries",
    "language": "Languages",
    "license": "Licenses",
    "tag": "Tags",
    "size": "Dataset size",
    "format": "Formats",
    "modality": "Modalities",
    "sdk": "SDKs",
}
FIELDS = {
    "task": ("pipeline_tag", "task_categories"),
    "library": ("library_name",),
    "language": ("language",),
    "license": ("license",),
    "tag": ("tags",),
    "size": ("size_categories",),
    "format": ("format", "formats"),
    "modality": ("modality", "modalities"),
    "sdk": ("sdk",),
}
_batches: set[asyncio.Task] = set()


class CardLoader(yaml.SafeLoader):
    """No aliases, recursive objects or unbounded YAML node expansion."""

    def compose_node(self, parent, index):
        self.card_nodes = getattr(self, "card_nodes", 0) + 1
        self.card_depth = getattr(self, "card_depth", 0) + 1
        if self.card_nodes > 2048 or self.card_depth > 20 or self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("Card metadata exceeds parsing limits")
        try:
            return super().compose_node(parent, index)
        finally:
            self.card_depth -= 1


CardLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
CardLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)


def parse_card(content: bytes) -> tuple[dict, dict[str, list[str]]]:
    """Only declared README metadata supplies facets; never fetch referenced URLs."""
    empty = {key: [] for key in FACETS}
    try:
        text = content[:MAX_README_BYTES].decode("utf-8-sig", errors="replace")
    except UnicodeDecodeError:
        return {}, empty
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, empty
    closing = next(
        (n for n, line in enumerate(lines[1:], 1) if line.rstrip() in {"---", "..."}), None
    )
    if closing is None:
        return {}, empty
    try:
        raw = yaml.load("\n".join(lines[1:closing]), Loader=CardLoader)
    except (yaml.YAMLError, ValueError, RecursionError):
        return {}, empty
    if not isinstance(raw, dict):
        return {}, empty
    metadata = {}
    for field in {name for names in FIELDS.values() for name in names} | {
        "base_model",
        "datasets",
        "license_name",
    }:
        value = raw.get(field)
        candidates = value if isinstance(value, list) else [value]
        values = []
        for candidate in candidates[:100]:
            if not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            if (
                candidate
                and len(candidate) <= 200
                and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in candidate)
            ):
                if candidate not in values:
                    values.append(candidate)
        if values:
            metadata[field] = values
    facets = {
        key: sorted({value.casefold() for name in names for value in metadata.get(name, [])})
        for key, names in FIELDS.items()
    }
    for tag in metadata.get("tags", []):
        for key in ("format", "modality"):
            prefix = key + ":"
            if tag.casefold().startswith(prefix) and (
                value := tag[len(prefix) :].strip().casefold()
            ):
                if value not in facets[key]:
                    facets[key].append(value)
    return metadata, facets


def mark_dirty(repo_id: int) -> None:
    """A main move invalidates old facets immediately without doing network I/O."""
    try:
        (
            M.insert(repository=repo_id)
            .on_conflict(
                conflict_target=[M.repository],
                update={
                    M.state: "pending",
                    M.generation: M.generation + 1,
                    M.retry_at: None,
                    M.lease_token: None,
                    M.lease_until: None,
                },
            )
            .execute()
        )
    except Exception as exc:
        # Never turn a committed upload into an API failure; TTL repairs missed invalidations.
        logger.warning(f"Could not invalidate repository metadata {repo_id}: {type(exc).__name__}")


def _ready_ids():
    # Expired snapshots are refresh candidates, but stay usable until a known
    # main move invalidates them. TTL alone must not make the catalog disappear.
    return M.select(M.repository).where(M.state == "ready")


def _due_catalog(scope):
    now = utcnow()
    return (
        Repository.select(Repository.id)
        .join(M, JOIN.LEFT_OUTER, on=(M.repository == Repository.id))
        .where(Repository.id.in_(scope.select(Repository.id)))
        .where(
            M.repository.is_null()
            | (M.state != "ready")
            | M.checked_at.is_null()
            | (M.checked_at <= now - REFRESH_AFTER)
        )
    )


def indexing_progress(scope) -> dict:
    return {"pending": _due_catalog(scope).count(), "total": scope.count()}


def _claim(repo_id: int) -> tuple[str, int] | None:
    now = utcnow()
    M.insert(repository=repo_id).on_conflict_ignore().execute()
    token = str(uuid4())
    claimed = (
        M.update(lease_token=token, lease_until=now + LEASE_AFTER)
        .where(
            (M.repository == repo_id)
            & (M.lease_until.is_null() | (M.lease_until <= now))
            & (M.retry_at.is_null() | (M.retry_at <= now))
        )
        .execute()
    )
    if not claimed:
        return None
    return token, M.get_by_id(repo_id).generation


async def read_card_prefix(client, source: str, head: str) -> bytes:
    """Try the declared names in order, sharing one bounded read deadline."""

    async def read_candidates():
        for path in README_CANDIDATES:
            try:
                return await client.get_object_prefix(source, head, path, MAX_README_BYTES)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        return b""

    return await asyncio.wait_for(read_candidates(), README_TIMEOUT_SECONDS)


async def index_repository(repo_id: int) -> None:
    repo = Repository.get_or_none(Repository.id == repo_id)
    if repo is None:
        return
    claimed = _claim(repo_id)
    if claimed is None:
        return
    token, generation = claimed
    fence = (M.repository == repo_id) & (M.lease_token == token) & (M.generation == generation)
    client = get_lakefs_client()
    source = resolve_lakefs_repo(repo)
    prior = M.get_by_id(repo_id)
    keep_ready = prior.state == "ready"
    try:
        head = (await asyncio.wait_for(client.get_branch(repository=source, branch="main"), 3))[
            "commit_id"
        ]
        if keep_ready and (prior.main_sha != head or prior.source_repo != source):
            M.update(state="pending").where(fence).execute()
            keep_ready = False
        content = b""
        if head:
            content = await read_card_prefix(client, source, head)
        metadata, facets = parse_card(content)
        actual = (await asyncio.wait_for(client.get_branch(repository=source, branch="main"), 3))[
            "commit_id"
        ]
        current = Repository.get_or_none(Repository.id == repo_id)
        if current is None:
            return
        if actual != head or resolve_lakefs_repo(current) != source:
            M.update(state="pending", lease_token=None, lease_until=None).where(fence).execute()
            return
        with M._meta.database.atomic():
            published = (
                M.update(
                    main_sha=head,
                    source_repo=source,
                    metadata=json.dumps(metadata),
                    state="ready",
                    checked_at=utcnow(),
                    retry_at=None,
                    lease_token=None,
                    lease_until=None,
                )
                .where(fence)
                .execute()
            )
            if not published:
                return
            F.delete().where(F.repository == repo_id).execute()
            rows = [
                {"repository": repo_id, "key": key, "value": value}
                for key, values in facets.items()
                for value in values
            ]
            # Keep below SQLite's historic 999-bind parameter limit as well.
            for start in range(0, len(rows), 250):
                F.insert_many(rows[start : start + 250]).execute()
    except asyncio.CancelledError:
        M.update(lease_token=None, lease_until=None).where(fence).execute()
        raise
    except Exception as exc:
        M.update(
            state="ready" if keep_ready else "error",
            retry_at=utcnow() + RETRY_AFTER,
            lease_token=None,
            lease_until=None,
        ).where(fence).execute()
        logger.debug(f"Metadata indexing failed for repository {repo_id}: {type(exc).__name__}")


def schedule_indexing(scope) -> None:
    """At most one forty-row job per API process; database leases fence other processes."""
    if _batches:
        return
    now = utcnow()
    ids = [
        r.id
        for r in _due_catalog(scope)
        .where(
            (M.retry_at.is_null() | (M.retry_at <= now))
            & (M.lease_until.is_null() | (M.lease_until <= now))
        )
        .order_by(Repository.id)
        .limit(40)
    ]
    if not ids:
        return

    async def run():
        semaphore = asyncio.Semaphore(4)

        async def bounded(repo_id):
            async with semaphore:
                await index_repository(repo_id)

        try:
            await asyncio.wait_for(
                asyncio.gather(*(bounded(repo_id) for repo_id in ids), return_exceptions=True), 25
            )
        except asyncio.TimeoutError:
            pass
        except Exception as exc:
            logger.warning(f"Discovery batch failed: {type(exc).__name__}")

    job = asyncio.create_task(run())
    _batches.add(job)
    job.add_done_callback(_batches.discard)


async def close_indexing() -> None:
    jobs = list(_batches)
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    _batches.clear()


def apply_filters(scope, filters: dict, skip: str | None = None):
    for key, values in filters.items():
        if values and key != skip:
            scope = scope.where(
                Repository.id.in_(
                    F.select(F.repository).where((F.key == key) & F.value.in_(values))
                )
            ).where(Repository.id.in_(_ready_ids()))
    return scope


def facet_options(scope, filters: dict) -> list[dict]:
    result = []
    for key, label in FACETS.items():
        candidates = apply_filters(scope, filters, skip=key)
        counts = {
            value: count
            for value, count in (
                F.select(F.value, fn.COUNT(F.repository).alias("count"))
                .where(
                    (F.key == key)
                    & F.repository.in_(candidates.select(Repository.id))
                    & F.repository.in_(_ready_ids())
                )
                .group_by(F.value)
                .tuples()
            )
        }
        for value in filters.get(key, []):
            counts.setdefault(value, 0)
        result.append(
            {
                "key": key,
                "label": label,
                "options": [
                    {"value": value, "count": count}
                    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                ],
            }
        )
    return result


def sorted_page(scope, repo_type: str, sort: str, limit: int, offset: int) -> list:
    if sort == "trending":
        from kohakuhub.api.utils.trending import calculate_trending_scores

        scores = calculate_trending_scores(repo_type)
        # Existing score algorithm, applied AFTER visibility and facet filtering.
        # Rank only scalar IDs/dates, never loading cards or making N LakeFS calls.
        candidates = list(scope.select(Repository.id, Repository.created_at).tuples())
        candidates.sort(key=lambda row: (scores.get(row[0], 0), row[1], row[0]), reverse=True)
        ids = [repo_id for repo_id, _ in candidates[offset : offset + limit]]
        rows = {row.id: row for row in Repository.select().where(Repository.id.in_(ids))}
        return [rows[repo_id] for repo_id in ids]
    if sort == "updated":
        newest = (
            Commit.select(Commit.repository.alias("rid"), fn.MAX(Commit.created_at).alias("at"))
            .where(Commit.branch == "main")
            .group_by(Commit.repository)
            .alias("newest")
        )
        scope = scope.join(newest, JOIN.LEFT_OUTER, on=(Repository.id == newest.c.rid))
        ordering = fn.COALESCE(newest.c.at, Repository.created_at).desc()
    else:
        ordering = {
            "likes": Repository.likes_count,
            "downloads": Repository.downloads,
            "recent": Repository.created_at,
        }[sort].desc()
    return list(scope.order_by(ordering, Repository.id.desc()).offset(offset).limit(limit))


def serialize_items(rows: list) -> list[dict]:
    from kohakuhub.api.repo.routers.info import _latest_main_commits

    ids = [row.id for row in rows]
    heads = _latest_main_commits(ids)
    records = {
        row.repository_id: row
        for row in M.select().where(M.repository.in_(ids) & M.repository.in_(_ready_ids()))
    }
    facets = {repo_id: {key: [] for key in FACETS} for repo_id in ids}
    for repo_id, key, value in (
        F.select(F.repository, F.key, F.value).where(F.repository.in_(list(records))).tuples()
    ):
        facets[repo_id][key].append(value)
    result = []
    for row in rows:
        record = records.get(row.id)
        metadata = json.loads(record.metadata) if record else {}
        result.append(
            {
                "id": row.full_id,
                "author": row.namespace,
                "private": row.private,
                "sha": record.main_sha if record else None,
                "createdAt": row.created_at.isoformat(),
                "lastModified": (
                    heads[row.id][1].isoformat() if row.id in heads else row.created_at.isoformat()
                ),
                "downloads": row.downloads,
                "likes": row.likes_count,
                "gated": False,
                "tags": metadata.get("tags", []),
                "metadata": metadata,
                "facets": facets[row.id],
            }
        )
    return result
