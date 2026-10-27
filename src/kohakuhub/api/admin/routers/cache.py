"""Admin endpoints for inspecting Redis and bounded local fallback caches.

Exposes hit/miss/error counters per cache namespace, Valkey memory
utilization, eviction count, and the bootstrap-flush metadata. These are
operational signals — they are reset on API process restart by design.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kohakuhub.api.admin.utils import verify_admin_token
from kohakuhub.cache import (
    get_memory_info,
    get_metrics_snapshot,
    get_runtime_snapshot,
    clear_process_cache,
    reset_metrics,
)
from kohakuhub.logger import get_logger

logger = get_logger("ADMIN")
router = APIRouter()


class CacheProcessTarget(BaseModel):
    instance_id: str = Field(min_length=1, max_length=128)


def _check_process(target: CacheProcessTarget | None) -> str:
    instance_id = get_runtime_snapshot()["backend"]["instance_id"]
    if target is not None and target.instance_id != instance_id:
        raise HTTPException(status_code=409, detail="A different worker received this request. Refresh the cache panel and retry.")
    return instance_id


@router.get("/cache/stats")
async def get_cache_stats(_admin: bool = Depends(verify_admin_token)):
    """Return cache hit/miss/error counters and Valkey memory state."""
    metrics = get_metrics_snapshot()
    memory = await get_memory_info()
    return {
        "metrics": metrics,
        "memory": memory,
        **get_runtime_snapshot(),
    }


@router.post("/cache/metrics/reset")
async def reset_cache_metrics(target: CacheProcessTarget | None = None, _admin: bool = Depends(verify_admin_token)):
    """Zero out the in-process metric counters.

    Useful when you want to measure the effect of a configuration change
    without restarting the API process. Does NOT touch cache contents.
    """
    _check_process(target)
    reset_metrics()
    return {"reset": True}


@router.post("/cache/local/clear")
async def clear_local_cache(target: CacheProcessTarget, _admin: bool = Depends(verify_admin_token)):
    """Clear only the process whose metrics the operator inspected."""
    instance_id = _check_process(target)
    return {"cleared": clear_process_cache(), "instance_id": instance_id, "scope": "process"}
