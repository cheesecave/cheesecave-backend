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
- What each branch head links is recorded in ``lfs_head_ref`` and never
  collected; keep windows only decide how many older versions stay. Commits
  (``update_head_refs``) and branch operations (``replace_head_refs``) keep
  it current. Earlier versions recorded none of it, so with ``lfs_auto_gc``
  on nothing is collected until ``storage.reconcile_lfs_references`` has
  recorded every branch head once (``references_reconciled``).

Keep this module free of ``kohakuhub.api`` imports: the worker imports it.
See issue #114.
"""

import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from peewee import PostgresqlDatabase

from kohakuhub.config import cfg
from kohakuhub.db import (
    File,
    LFSObjectHistory,
    LfsGcCandidate,
    LfsGcState,
    LfsHeadRef,
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
RECONCILED_KEY = "references_reconciled_at"
_LFS_ADDRESS = re.compile(r"^s3://[^/]+/lfs/[0-9a-f]{2}/[0-9a-f]{2}/([0-9a-f]{64})$")


class LfsObjectUnavailable(Exception):
    """The LFS object is being collected or is gone; upload it again."""


def lfs_key(sha256: str) -> str:
    return f"lfs/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def lfs_oid(physical_address: str | None) -> str | None:
    """The sha256 of a global LFS object address, or ``None`` for any other.

    The address is what a LakeFS entry really links, so it is the identity
    to trust over a checksum or a size-based LFS rule.
    """
    match = _LFS_ADDRESS.match(physical_address or "")
    return match.group(1) if match else None


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
    - ``head``: the head of a branch of any repository links it;
    - ``file``: an active file in any repository has this content;
    - ``history``: a history row references it. With ``lfs_auto_gc`` on,
      only while it is among the newest ``keep_versions`` unique versions
      of that repository path; with it off every version is kept.
    """
    now = now or utcnow()
    if LfsRecentObject.get_or_none(
        (LfsRecentObject.sha256 == sha256) & (LfsRecentObject.touched_at >= now - RECENT_GRACE)
    ):
        return "recent"
    # Any active file with this content, whatever its LFS flag says: revert
    # and reset recompute the flag from size rules that may have changed.
    if LfsHeadRef.select().where(LfsHeadRef.sha256 == sha256).exists():
        return "head"
    if File.select().where((File.sha256 == sha256) & (File.is_deleted == False)).exists():
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


def references_reconciled() -> bool:
    """Whether every branch head's LFS references were reconciled once."""
    return LfsGcState.get_or_none(LfsGcState.key == RECONCILED_KEY) is not None


def mark_references_reconciled() -> None:
    now = utcnow()
    stamp = now.replace(tzinfo=timezone.utc).isoformat()
    LfsGcState.insert(key=RECONCILED_KEY, value=stamp, updated_at=now).on_conflict(
        conflict_target=[LfsGcState.key],
        update={LfsGcState.value: stamp, LfsGcState.updated_at: now},
    ).execute()


def reconciled_since() -> datetime:
    """When the last reconciliation completed (call once one has)."""
    return LfsGcState.get(LfsGcState.key == RECONCILED_KEY).updated_at


def reconciled_at() -> str | None:
    row = LfsGcState.get_or_none(LfsGcState.key == RECONCILED_KEY)
    return row.value if row else None


def _head_refs(repo: Repository, branch: str | None = None):
    R = LfsHeadRef
    where = R.repository == repo
    return where if branch is None else where & (R.branch == branch)


def add_head_refs(repo: Repository, refs: Iterable[tuple[str, str, str]]) -> int:
    """Record ``(branch, path, sha256)`` links a branch head has; returns how many were new.

    Only adds: a link recorded already, or one a concurrent commit has just
    replaced, is left as it is (an extra row only keeps an object longer).
    """
    R = LfsHeadRef
    refs = set(refs)
    paths = sorted({path for _, path, _ in refs})
    # Look up only the paths involved, so a commit costs what it changes
    existing = {
        row
        for start in range(0, len(paths), CANDIDATE_BATCH)
        for row in R.select(R.branch, R.path_in_repo, R.sha256)
        .where(_head_refs(repo) & R.path_in_repo.in_(paths[start : start + CANDIDATE_BATCH]))
        .tuples()
    }
    now = utcnow()
    rows = [
        {"repository": repo.id, "branch": b, "path_in_repo": p, "sha256": s, "created_at": now}
        for b, p, s in sorted(refs - existing)
    ]
    for start in range(0, len(rows), CANDIDATE_BATCH):
        R.insert_many(rows[start : start + CANDIDATE_BATCH]).on_conflict_ignore().execute()
    return len(rows)


def update_head_refs(
    repo: Repository,
    branch: str,
    paths: dict[str, str | None],
    folders: Iterable[str] = (),
) -> set[str]:
    """Record what a commit to ``branch`` changed: each path in ``paths`` now
    links that sha256 (``None``: no LFS object), and nothing under ``folders``.

    Returns the objects the branch no longer links; they may be garbage now.
    """
    R = LfsHeadRef
    listed = sorted(paths)
    new = {(path, sha) for path, sha in paths.items() if sha}
    fields = (R.id, R.path_in_repo, R.sha256)
    with _database().atomic():
        rows = [
            row
            for start in range(0, len(listed), CANDIDATE_BATCH)
            for row in R.select(*fields)
            .where(
                _head_refs(repo, branch)
                & R.path_in_repo.in_(listed[start : start + CANDIDATE_BATCH])
            )
            .tuples()
        ]
        for folder in folders:
            # startswith is ILIKE: narrow in SQL, match case-sensitively here
            rows += [
                row
                for row in R.select(*fields)
                .where(_head_refs(repo, branch) & R.path_in_repo.startswith(folder))
                .tuples()
                if row[1].startswith(folder)
            ]
        ids = sorted({row[0] for row in rows})
        for start in range(0, len(ids), CANDIDATE_BATCH):
            R.delete().where(R.id.in_(ids[start : start + CANDIDATE_BATCH])).execute()
        add_head_refs(repo, ((branch, path, sha) for path, sha in new))
    return {sha for _, _, sha in rows} - {sha for _, sha in new}


def replace_head_refs(
    repo: Repository, branch: str | None, refs: Iterable[tuple[str, str, str]]
) -> set[str]:
    """Make ``branch``'s links (every branch's, for ``None``) exactly ``refs``,
    after an operation that rewrote the head (revert, merge, reset, a repository
    migration). Returns the objects no longer linked.
    """
    R = LfsHeadRef
    refs = set(refs)
    with _database().atomic():
        old = set(
            R.select(R.branch, R.path_in_repo, R.sha256).where(_head_refs(repo, branch)).tuples()
        )
        R.delete().where(_head_refs(repo, branch)).execute()
        add_head_refs(repo, refs)
    return {sha for _, _, sha in old} - {sha for _, _, sha in refs}


def reconcile_references(
    repo: Repository,
    heads: set[tuple[str, str, str]],
    default_head: dict[str, tuple[str, int]],
    default_paths: Iterable[str] = (),
) -> dict[str, int]:
    """Make the database account for the LFS objects ``repo``'s branches link.

    ``heads`` holds every ``(branch, path, sha256)`` link of every branch
    head; ``default_head`` maps each path of the default branch's head to its
    ``(sha256, size)``. It only adds or corrects rows, never deletes, and
    changes nothing when everything already accounts (running it again is a
    no-op):

    - every link gets its ``lfs_head_ref`` row, so no branch head can lose
      content, whatever the keep windows say;
    - the default branch's file rows get the linked sha256, size and LFS
      flag (``copyFile`` recorded the source's current sha256, revert and
      reset derived the flag from size rules);
    - a file row marked deleted while its path is on the default branch
      head (``default_paths``, every object there) is active again: deleting
      a folder also marked folders differing only in case deleted.

    Returns counts of what it added or corrected.
    """
    default_paths = set(default_paths)
    restored = [
        row.id
        for row in File.select(File.id, File.path_in_repo).where(
            (File.repository == repo) & (File.is_deleted == True)
        )
        if row.path_in_repo in default_paths and row.path_in_repo not in default_head
    ]
    files = {
        row.path_in_repo: row
        for start in range(0, len(default_head), CANDIDATE_BATCH)
        for row in File.select().where(
            (File.repository == repo)
            & File.path_in_repo.in_(sorted(default_head)[start : start + CANDIDATE_BATCH])
        )
    }
    now = datetime.now(timezone.utc)  # like the file rows' defaults
    fixed = 0
    with _database().atomic():
        added = add_head_refs(repo, heads)
        for path, (sha, size) in sorted(default_head.items()):
            row = files.get(path)
            if row is None:
                File.create(
                    repository=repo,
                    path_in_repo=path,
                    sha256=sha,
                    size=size,
                    lfs=True,
                    owner=repo.owner_id,
                )
            elif (row.sha256, row.size, row.lfs, row.is_deleted) != (sha, size, True, False):
                File.update(
                    sha256=sha, size=size, lfs=True, is_deleted=False, updated_at=now
                ).where(File.id == row.id).execute()
            else:
                continue
            fixed += 1
        for start in range(0, len(restored), CANDIDATE_BATCH):
            File.update(is_deleted=False, updated_at=now).where(
                File.id.in_(restored[start : start + CANDIDATE_BATCH])
            ).execute()
    return {"head_refs_added": added, "files_fixed": fixed + len(restored)}


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
