"""Remove a duplicate's destination a crash left half built.

A duplicate holds its new repository under the lock named ``duplicate``
(``api.repo.routers.duplicate``), renewed while it works and released when it
ends, however it ends. A process that died never released: the lock lapsed and
its token stayed. A repository still carrying one, long after it lapsed, is
such a leftover.

Keep this module free of ``kohakuhub.api`` imports: the worker imports it.
"""

from datetime import timedelta
from typing import Any

from kohakuhub import tasks
from kohakuhub.db import Repository, utcnow
from kohakuhub.db_operations import delete_repository
from kohakuhub.logger import get_logger

logger = get_logger("DUPLICATE_SWEEP")

SWEEP_KIND = "duplicate.sweep"
# After the lock lapsed: a slow but live duplicate renews it, so this is only margin
GRACE = timedelta(minutes=5)


def leftovers() -> list[Repository]:
    R = Repository
    return list(
        R.select().where(
            R.operation.startswith("duplicate:")
            & R.operation_until.is_null(False)
            & (R.operation_until < utcnow() - GRACE)
        )
    )


@tasks.task(SWEEP_KIND, every=timedelta(minutes=5), max_attempts=3)
async def sweep(payload: dict[str, Any]) -> None:
    for repo in leftovers():
        logger.warning(f"Removing {repo.full_id}: a duplicate into it died half way")
        delete_repository(repo)
