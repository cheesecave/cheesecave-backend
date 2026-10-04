"""The main site's paginated discovery catalog; HF list APIs retain their wire contract."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from kohakuhub.auth.permissions import filter_readable_repositories
from kohakuhub.auth.dependencies import get_optional_user
from kohakuhub.db import Repository, User
from kohakuhub import repository_discovery as discovery

router = APIRouter()


@router.get("/{plural}/discover")
async def discover_repositories(
    plural: Literal["models", "datasets", "spaces"],
    response: Response,
    search: str = Query("", max_length=200),
    sort: Literal["trending", "recent", "updated", "likes", "downloads"] = "trending",
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task: list[str] = Query([]),
    library: list[str] = Query([]),
    language: list[str] = Query([]),
    license: list[str] = Query([]),
    tag: list[str] = Query([]),
    size: list[str] = Query([]),
    format: list[str] = Query([]),
    modality: list[str] = Query([]),
    sdk: list[str] = Query([]),
    user: User | None = Depends(get_optional_user),
):
    values = dict(
        task=task,
        library=library,
        language=language,
        license=license,
        tag=tag,
        size=size,
        format=format,
        modality=modality,
        sdk=sdk,
    )
    for key, selected in values.items():
        if len(selected) > 20 or any(
            not v.strip() or len(v) > 200 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in v)
            for v in selected
        ):
            raise HTTPException(422, detail=f"Invalid {key} filter")
        values[key] = list(dict.fromkeys(value.strip().casefold() for value in selected))
    repo_type = plural[:-1]
    scope = filter_readable_repositories(
        Repository.select().where(Repository.repo_type == repo_type), user
    )
    if search.strip():
        scope = scope.where(Repository.full_id.contains(search.strip()))
    filtered = discovery.apply_filters(scope, values)
    total = filtered.count()
    rows = discovery.sorted_page(filtered, repo_type, sort, limit, offset)
    result = {
        "items": discovery.serialize_items(rows),
        "total": total,
        "facets": discovery.facet_options(scope, values),
        "has_more": offset + len(rows) < total,
        "indexing": discovery.indexing_progress(scope),
        "selected": values,
    }
    discovery.schedule_indexing(scope)
    response.headers["Cache-Control"] = "no-store"
    return result
