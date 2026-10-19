"""One call for the admin Overview (issue cheesecave-admin#20)."""

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from kohakuhub import admin_overview
from kohakuhub.api.admin.utils import verify_admin_token

router = APIRouter()

SCHEMA = 1


@router.get("/overview")
async def get_overview(
    response: Response,
    sections: str | None = Query(
        default=None,
        description="Comma-separated section names; omitted means all. Unknown names are ignored.",
    ),
    range_: Literal["24h", "7d", "30d"] = Query(default="7d", alias="range"),
    _admin: bool = Depends(verify_admin_token),
):
    """Status, what needs attention, counts, activity and recent events in one response.

    Sections are independent: each carries its own ``generated_at`` and ``cache`` state, and one that
    fails reports ``{"error": {...}}`` while the others are still returned (HTTP 200).
    """
    response.headers["Cache-Control"] = "no-store"
    wanted = list(admin_overview.SECTIONS) if sections is None else [s.strip() for s in sections.split(",")]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "range": range_,
        "sections": await admin_overview.collect(wanted, range_),
    }
