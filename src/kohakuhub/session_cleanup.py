"""Delete login sessions that have expired.

Nothing deleted an expired session, so the rows only piled up. A session past its
``expires_at`` is already refused at login check time; this only frees the row.

Keep this module free of ``kohakuhub.api`` imports: the worker imports it.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from kohakuhub import tasks
from kohakuhub.db import Session
from kohakuhub.logger import get_logger

logger = get_logger("SESSION_CLEANUP")

EXPIRE_KIND = "auth.expire_sessions"


@tasks.task(EXPIRE_KIND, every=timedelta(hours=1), max_attempts=3)
async def expire_sessions(payload: dict[str, Any]) -> None:
    # Same aware-UTC comparison the authentication check uses.
    deleted = (
        Session.delete().where(Session.expires_at <= datetime.now(timezone.utc)).execute()
    )
    if deleted:
        logger.info(f"Deleted {deleted} expired session(s)")
