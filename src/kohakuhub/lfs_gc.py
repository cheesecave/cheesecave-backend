"""LFS garbage collection: when an LFS object may go, and deleting it safely.

LFS objects are content-addressed and shared: ``lfs/aa/bb/<sha256>`` is stored
once for every repository, path and version with the same content. So one
decision, ``retention_reason``, answers "is anything still relying on this
object?" for every deletion path, using index lookups proportional to the
rows that reference the object (never a scan of a repository or the bucket).

- Candidates are recorded where an object may have become garbage, in the
  same transaction as the change (``record_candidates``); the
  ``storage.collect_lfs`` task decides and deletes them in the background.
- Deleting writes a tombstone first (``begin_delete``: deleting, then
  ``finish_delete``: deleted). History rows are kept, so recoverability and
  quota ask the tombstones what is gone (``deleted_shas``).
- Uploads touch ``lfs_recent_object`` and commits claim an object under the
  same per-object lock the collector re-decides under, so a collection never
  deletes content an upload or a commit in flight relies on; a commit that
  meets a tombstone is told to upload again (``claim_for_commit``).

Keep this module free of ``kohakuhub.api`` imports: the worker imports it.
See issue #114.
"""

from collections.abc import Iterable
from datetime import datetime, timedelta

from peewee import PostgresqlDatabase

from kohakuhub.config import cfg
from kohakuhub.db import (
    File,
    LFSObjectHistory,
    LfsGcCandidate,
    LfsObjectTombstone,
    LfsRecentObject,
    Repository,
    utcnow,
)

DELETING = "deleting"
DELETED = "deleted"
# How long an upload or a commit claim keeps an object; long enough for any
# upload-then-commit round trip, short enough to collect abandoned uploads.
RECENT_GRACE = timedelta(hours=24)
CANDIDATE_BATCH = 500


class LfsObjectUnavailable(Exception):
    """The LFS object is being collected or is gone; upload it again."""


def lfs_key(sha256: str) -> str:
    return f"lfs/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def keep_versions(repo: Repository) -> int:
    """The number of unique versions per path this repository keeps."""
    if repo.lfs_keep_versions is not None:
        return repo.lfs_keep_versions
    return cfg.app.lfs_keep_versions


def _database():
    return LfsObjectTombstone._meta.database


def _lock(sha256: str) -> None:
    """Serialize deciding and claiming one object; call inside a transaction.

    SQLite serializes writers already, so it needs no lock.
    """
    database = _database()
    if isinstance(database, PostgresqlDatabase):
        database.execute_sql("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"lfs:{sha256}",))


def _unique_versions(repository_id: int, path: str) -> list[str]:
    """This path's unique LFS versions, newest first."""
    H = LFSObjectHistory
    seen: list[str] = []
    # ponytail: reads a path's whole history; a path with thousands of
    # versions would want a LIMIT with a window function instead.
    for (sha256,) in (
        H.select(H.sha256)
        .where((H.repository == repository_id) & (H.path_in_repo == path))
        .order_by(H.created_at.desc(), H.id.desc())
        .tuples()
    ):
        if sha256 not in seen:
            seen.append(sha256)
    return seen


def retention_reason(sha256: str, now: datetime | None = None) -> str | None:
    """Why ``sha256`` must be kept, or ``None`` when nothing relies on it.

    - ``recent``: uploaded or claimed by a commit within ``RECENT_GRACE``;
    - ``file``: an active LFS file in any repository uses it;
    - ``history``: a history row references it. With ``lfs_auto_gc`` on,
      only while it is among the newest ``keep_versions`` unique versions
      of that repository path; with it off every version is kept.
    """
    now = now or utcnow()
    if LfsRecentObject.get_or_none(
        (LfsRecentObject.sha256 == sha256) & (LfsRecentObject.touched_at >= now - RECENT_GRACE)
    ):
        return "recent"
    if (
        File.select()
        .where((File.sha256 == sha256) & (File.lfs == True) & (File.is_deleted == False))
        .exists()
    ):
        return "file"
    H = LFSObjectHistory
    references = list(
        H.select(H.repository, H.path_in_repo).where(H.sha256 == sha256).distinct().tuples()
    )
    if not references:
        return None
    if not cfg.app.lfs_auto_gc:
        return "history"
    repos = {
        repo.id: repo
        for repo in Repository.select().where(Repository.id.in_({r for r, _ in references}))
    }
    for repository_id, path in references:
        if sha256 in _unique_versions(repository_id, path)[: keep_versions(repos[repository_id])]:
            return "history"
    return None


def record_candidates(shas: Iterable[str]) -> int:
    """Record objects that may have become garbage; returns how many."""
    rows = [{"sha256": sha} for sha in sorted(set(shas))]
    for start in range(0, len(rows), CANDIDATE_BATCH):
        LfsGcCandidate.insert_many(
            rows[start : start + CANDIDATE_BATCH]
        ).on_conflict_ignore().execute()
    return len(rows)


def evicted_versions(repo: Repository, path: str) -> list[str]:
    """This path's versions beyond the repository's keep window."""
    return _unique_versions(repo.id, path)[keep_versions(repo) :]


def record_evicted_versions(repo: Repository, paths: Iterable[str]) -> int:
    """Record versions a commit pushed out of these paths' keep windows.

    Only with ``lfs_auto_gc`` on: otherwise every version is kept.
    """
    if not cfg.app.lfs_auto_gc:
        return 0
    return record_candidates(sha for path in set(paths) for sha in evicted_versions(repo, path))


def touch(sha256: str, now: datetime | None = None) -> None:
    """Keep ``sha256`` through the grace period (an upload is starting)."""
    now = now or utcnow()
    LfsRecentObject.insert(sha256=sha256, touched_at=now).on_conflict(
        conflict_target=[LfsRecentObject.sha256], update={LfsRecentObject.touched_at: now}
    ).execute()


def tombstone_state(sha256: str) -> str | None:
    tombstone = LfsObjectTombstone.get_or_none(LfsObjectTombstone.sha256 == sha256)
    return tombstone.state if tombstone else None


def claim_for_commit(sha256: str, exists_in_storage: bool) -> bool:
    """Protect ``sha256`` while a commit links it; raise if it is not usable.

    Runs under the object's lock, so it either sees a collection's tombstone
    or the collection sees this claim and keeps the object. Content that was
    collected and has since been uploaded again is revived; returns whether
    it was. The caller must then check storage again: its earlier check may
    predate the collection's delete, which is complete once ``deleted``.
    """
    with _database().atomic():
        _lock(sha256)
        state = tombstone_state(sha256)
        if state == DELETING or not exists_in_storage:
            raise LfsObjectUnavailable(sha256)
        if state == DELETED:
            LfsObjectTombstone.delete().where(LfsObjectTombstone.sha256 == sha256).execute()
        touch(sha256)
    return state == DELETED


def begin_delete(sha256: str) -> bool:
    """Decide under the object's lock and tombstone it if it may go.

    Returns whether the caller should delete the object from storage. An
    object that is relied on again loses a leftover ``deleting`` tombstone,
    since it was not deleted yet.
    """
    with _database().atomic():
        _lock(sha256)
        if retention_reason(sha256) is not None:
            LfsObjectTombstone.delete().where(
                (LfsObjectTombstone.sha256 == sha256) & (LfsObjectTombstone.state == DELETING)
            ).execute()
            return False
        now = utcnow()
        LfsObjectTombstone.insert(
            sha256=sha256, state=DELETING, created_at=now, updated_at=now
        ).on_conflict(
            conflict_target=[LfsObjectTombstone.sha256],
            update={LfsObjectTombstone.state: DELETING, LfsObjectTombstone.updated_at: now},
        ).execute()
        return True


def finish_delete(shas: Iterable[str]) -> None:
    shas = list(shas)
    if shas:
        LfsObjectTombstone.update(state=DELETED, updated_at=utcnow()).where(
            LfsObjectTombstone.sha256.in_(shas) & (LfsObjectTombstone.state == DELETING)
        ).execute()


def deleted_shas(shas: Iterable[str]) -> set[str]:
    """Which of ``shas`` garbage collection has deleted (or is deleting)."""
    shas = list(set(shas))
    T = LfsObjectTombstone
    found: set[str] = set()
    for start in range(0, len(shas), CANDIDATE_BATCH):
        found.update(
            sha
            for (sha,) in T.select(T.sha256)
            .where(T.sha256.in_(shas[start : start + CANDIDATE_BATCH]))
            .tuples()
        )
    return found


def expire_recent(now: datetime | None = None, batch: int = CANDIDATE_BATCH) -> int:
    """Turn objects whose grace period ended into candidates; returns how many.

    Committed objects are then kept by their references; an upload that was
    never committed has none and is collected.
    """
    now = now or utcnow()
    R = LfsRecentObject
    expired = 0
    while shas := [
        sha
        for (sha,) in R.select(R.sha256)
        .where(R.touched_at < now - RECENT_GRACE)
        .order_by(R.touched_at)
        .limit(batch)
        .tuples()
    ]:
        with _database().atomic():
            record_candidates(shas)
            R.delete().where(R.sha256.in_(shas) & (R.touched_at < now - RECENT_GRACE)).execute()
        expired += len(shas)
    return expired
