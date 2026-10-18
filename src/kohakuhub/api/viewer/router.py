"""HTTP surface of the data explorer: ``/api/{type}s/{ns}/{name}/viewer/...`` (Hugging Face-shaped answers) and the
``/api/datasets/{ns}/{name}/parquet`` compatibility listing that ``hf datasets parquet`` and DuckDB users rely on.

Every route checks read permission through the same helpers as ``/resolve`` (a private repository answers a stranger
as it answers for a missing one), takes a ``revision`` (a branch, tag or commit id, resolved once to a commit id and
echoed back so a link can be pinned), and reads only through bounded, counted Range reads.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from kohakuhub.api.viewer import service, splits
from kohakuhub.api.viewer.rangefile import BudgetExceeded
from kohakuhub.auth.dependencies import get_optional_user

router = APIRouter()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"error": message}, headers={"X-Error-Code": code, "X-Error-Message": message})


async def _resolved(repo_type, namespace, name, revision, user):
    ctx = await service.open_repo(repo_type, namespace, name, revision, user)
    files = await service.list_files(ctx)
    readme = await service.read_text(ctx, "README.md") if any(f["path"] == "README.md" for f in files) else None
    return ctx, files, splits.resolve(files, readme)


def _pick(resolution: dict, config: str | None, split: str | None):
    configs = resolution["configs"]
    if not configs:
        raise _error(404, "ViewerNotAvailable", "this repository has no data files the explorer can read")
    chosen = next((c for c in configs if c["config"] == config), None) if config else next((c for c in configs if c["default"]), configs[0])
    if chosen is None:
        raise _error(404, "ConfigNotFound", f"no config {config!r}; available: {[c['config'] for c in configs]}")
    sp = next((s for s in chosen["splits"] if s["split"] == split), None) if split else chosen["splits"][0]
    if sp is None:
        raise _error(404, "SplitNotFound", f"no split {split!r} in config {chosen['config']!r}; available: {[s['split'] for s in chosen['splits']]}")
    return chosen, sp


async def _refs(ctx, sp):
    return [await service.locate(ctx, f["path"]) for f in sp["files"]]


@router.get("/{repo_type}s/{namespace}/{name}/viewer/splits")
async def viewer_splits(repo_type: str, namespace: str, name: str, revision: str = "main", user=Depends(get_optional_user)):
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    return {"revision": revision, "commit": ctx.commit, "source": resolution["source"], "configs": resolution["configs"]}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/is-valid")
async def viewer_is_valid(repo_type: str, namespace: str, name: str, revision: str = "main", user=Depends(get_optional_user)):
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    formats = {s["format"] for c in resolution["configs"] for s in c["splits"]}
    tabular = bool(formats & {"parquet", "csv", "jsonl", "json"})
    parquet = "parquet" in formats
    return {"viewer": tabular, "preview": tabular, "search": parquet, "filter": parquet, "statistics": parquet, "archives": "webdataset" in formats, "formats": sorted(formats)}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/size")
async def viewer_size(repo_type: str, namespace: str, name: str, revision: str = "main", user=Depends(get_optional_user)):
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    out = []
    for c in resolution["configs"]:
        for s in c["splits"]:
            entry = {"config": c["config"], "split": s["split"], "format": s["format"], "num_bytes": sum(f["size"] for f in s["files"]), "num_files": len(s["files"])}
            if s["format"] == "parquet":
                refs = await _refs(ctx, s)
                entry["num_rows"] = sum([(await service.aio(service.parquet_summary, r))["rows"] for r in refs])
            else:
                entry["num_rows"] = None
            out.append(entry)
    return {"commit": ctx.commit, "splits": out, "partial": False}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/rows")
async def viewer_rows(
    repo_type: str, namespace: str, name: str, config: str | None = None, split: str | None = None,
    offset: int = Query(0, ge=0), length: int = Query(100, ge=1), revision: str = "main", user=Depends(get_optional_user),
):
    if length > service.MAX_ROWS:
        raise _error(422, "InvalidParameter", f"length is at most {service.MAX_ROWS}")
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    chosen, sp = _pick(resolution, config, split)
    if sp["format"] not in ("parquet", "csv", "jsonl", "json"):
        raise _error(422, "UnsupportedFormat", f"rows are not available for {sp['format']} files; use the archive routes")
    refs = await _refs(ctx, sp)
    result = await service.aio(service.rows, sp["format"], refs, offset, length)
    return {
        "commit": ctx.commit, "config": chosen["config"], "split": sp["split"], "format": sp["format"],
        "features": service.features_list(result["columns"]), "rows": result["rows"],
        "num_rows_total": result["total"], "num_rows_per_page": service.MAX_ROWS, "partial": result["partial"],
        "cost": {"range_requests": result["requests"], "bytes_read": result["bytes"]},
    }


@router.get("/{repo_type}s/{namespace}/{name}/viewer/filter")
async def viewer_filter(
    repo_type: str, namespace: str, name: str, where: str, config: str | None = None, split: str | None = None,
    offset: int = Query(0, ge=0), length: int = Query(100, ge=1, le=service.MAX_ROWS), revision: str = "main", user=Depends(get_optional_user),
):
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    chosen, sp = _pick(resolution, config, split)
    if sp["format"] != "parquet":
        raise _error(422, "UnsupportedFormat", "filter needs parquet files")
    refs = await _refs(ctx, sp)
    try:
        result = await service.aio(service.filter_rows, refs, where, offset, length)
    except service.FilterError as e:
        raise _error(422, "InvalidFilter", str(e)) from e
    summary = await service.aio(service.parquet_summary, refs[0])
    return {"commit": ctx.commit, "features": service.features_list(summary["columns"]), "rows": result["rows"], "num_rows_total": None if result["partial"] else result["matches_seen"],
            "matches_seen": result["matches_seen"], "scanned_rows": result["scanned_rows"], "partial": result["partial"], "cost": {"range_requests": result["requests"], "bytes_read": result["bytes"]}}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/search")
async def viewer_search(
    repo_type: str, namespace: str, name: str, query: str, config: str | None = None, split: str | None = None,
    offset: int = Query(0, ge=0), length: int = Query(100, ge=1, le=service.MAX_ROWS), revision: str = "main", user=Depends(get_optional_user),
):
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    chosen, sp = _pick(resolution, config, split)
    if sp["format"] != "parquet":
        raise _error(422, "UnsupportedFormat", "search needs parquet files")
    refs = await _refs(ctx, sp)
    result = await service.aio(service.search_rows, refs, query, offset, length)
    summary = await service.aio(service.parquet_summary, refs[0])
    return {"commit": ctx.commit, "features": service.features_list(summary["columns"]), "rows": result["rows"], "num_rows_total": None if result["partial"] else result["matches_seen"],
            "matches_seen": result["matches_seen"], "scanned_rows": result["scanned_rows"], "partial": result["partial"], "cost": {"range_requests": result["requests"], "bytes_read": result["bytes"]}}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/statistics")
async def viewer_statistics(repo_type: str, namespace: str, name: str, config: str | None = None, split: str | None = None, revision: str = "main", user=Depends(get_optional_user)):
    ctx, _, resolution = await _resolved(repo_type, namespace, name, revision, user)
    chosen, sp = _pick(resolution, config, split)
    if sp["format"] != "parquet":
        raise _error(422, "UnsupportedFormat", "statistics need parquet files")
    refs = await _refs(ctx, sp)
    result = await service.aio(service.statistics, refs)
    return {"commit": ctx.commit, "config": chosen["config"], "split": sp["split"], **result}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/tar-index")
async def viewer_tar_index(repo_type: str, namespace: str, name: str, path: str, limit: int = Query(200, ge=1, le=2000), revision: str = "main", user=Depends(get_optional_user)):
    ctx = await service.open_repo(repo_type, namespace, name, revision, user)
    if not path.lower().endswith(".tar"):
        raise _error(422, "UnsupportedFormat", "tar-index reads .tar files")
    ref = await service.locate(ctx, path)
    index = await service.aio(service.tar_index, ref)
    return {"commit": ctx.commit, "path": path, "members": index["members"], "num_samples": len(index["samples"]), "samples": index["samples"][:limit],
            "truncated": index["truncated"], "cost": {"range_requests": index["requests"], "bytes_read": index["bytes"]}, "file_size": index["file_size"]}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/diff")
async def viewer_diff(repo_type: str, namespace: str, name: str, path: str, old: str, new: str, key: str | None = None, user=Depends(get_optional_user)):
    old_ctx = await service.open_repo(repo_type, namespace, name, old, user)
    new_ctx = await service.open_repo(repo_type, namespace, name, new, user)
    old_ref, new_ref = await service.locate(old_ctx, path), await service.locate(new_ctx, path)
    try:
        result = await service.aio(service.diff_tables, old_ref, new_ref, key)
    except BudgetExceeded as e:
        raise _error(413, "TooLarge", f"the diff reads both files and would exceed its budget: {e}") from e
    return {"path": path, "old": old_ctx.commit, "new": new_ctx.commit, **result}


@router.get("/{repo_type}s/{namespace}/{name}/viewer/_metrics")
async def viewer_metrics(repo_type: str, namespace: str, name: str):
    """Prototype only: cache counters, for the tests."""
    return dict(service.METRICS)


# ------------------------------------------------------------------------------------------------ Hub compatibility
compat = APIRouter()


@compat.get("/datasets/{namespace}/{name}/parquet")
async def parquet_listing(namespace: str, name: str, revision: str = "main", user=Depends(get_optional_user)):
    """``{config: {split: [url, ...]}}`` for the repository's native parquet files (what ``hf datasets parquet`` reads)."""
    ctx, _, resolution = await _resolved("dataset", namespace, name, revision, user)
    from kohakuhub.config import cfg
    base = cfg.app.base_url.rstrip("/")
    out: dict = {}
    for c in resolution["configs"]:
        for s in c["splits"]:
            if s["format"] == "parquet":
                out.setdefault(c["config"], {})[s["split"]] = [f"{base}/api/datasets/{namespace}/{name}/parquet/{c['config']}/{s['split']}/{i}.parquet" for i, _ in enumerate(s["files"])]
    return out


@compat.get("/datasets/{namespace}/{name}/parquet/{config}/{split}")
async def parquet_split(namespace: str, name: str, config: str, split: str, revision: str = "main", user=Depends(get_optional_user)):
    listing = await parquet_listing(namespace, name, revision, user)
    try:
        return listing[config][split]
    except KeyError as e:
        raise _error(404, "EntryNotFound", f"no parquet files for {config}/{split}") from e


@compat.get("/datasets/{namespace}/{name}/parquet/{config}/{split}/{n}.parquet")
async def parquet_file(namespace: str, name: str, config: str, split: str, n: int, revision: str = "main", user=Depends(get_optional_user)):
    """Redirects to ``/resolve/`` of the n-th file, as the Hub redirects to its converted files."""
    ctx, _, resolution = await _resolved("dataset", namespace, name, revision, user)
    chosen, sp = _pick(resolution, config, split)
    if sp["format"] != "parquet" or n >= len(sp["files"]):
        raise _error(404, "EntryNotFound", f"no parquet file {n} in {config}/{split}")
    return RedirectResponse(f"/datasets/{namespace}/{name}/resolve/{ctx.commit}/{sp['files'][n]['path']}", status_code=302)
