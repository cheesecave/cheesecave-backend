"""Delete admin sessions that can no longer be used.

A session past its idle or absolute end is already refused when it is checked; this only frees the row. A revoked
session stays a few days (the list of sessions can still say it was ended) and then goes too.

Keep this module free of ``kohakuhub.api`` imports: the worker imports it.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from kohakuhub import tasks
from kohakuhub.db import AdminSession
from kohakuhub.logger import get_logger

logger = get_logger("ADMIN_SESSION_CLEANUP")

EXPIRE_KIND = "admin.expire_sessions"
REVOKED_KEPT = timedelta(days=7)


def delete_unusable(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return (
        AdminSession.delete()
        .where(
            (AdminSession.expires_at <= now)
            | (AdminSession.absolute_expires_at <= now)
            | (AdminSession.revoked_at <= now - REVOKED_KEPT)
        )
        .execute()
    )


@tasks.task(EXPIRE_KIND, every=timedelta(hours=1), max_attempts=3)
async def expire_admin_sessions(payload: dict[str, Any]) -> None:
    deleted = delete_unusable()
    if deleted:
        logger.info(f"Deleted {deleted} admin session(s) that can no longer be used")
