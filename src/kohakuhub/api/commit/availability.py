"""Whether a commit can be reverted, or a branch reset to it, known up front.

Decided from what LakeFS and the bucket really hold:

- **Revert** of commit ``C`` onto branch ``B`` (first parent ``P``) is
  LakeFS's three-way revert: for every path ``C`` changed, ``B`` still at
  ``C``'s version is undone (to ``P``'s), ``B`` already at ``P``'s is left
  alone, anything else is a conflict. It needs ``P``'s versions of the
  paths it undoes; an initial commit has nothing to revert, and a revert
  that would change no file's content is pointless ("no_changes").
- **Reset** of ``B`` to ``C`` restores every path whose content differs
  (a two-dot diff), so it needs ``C``'s LFS objects of those paths. This is
  what a reset must do; the reset endpoint still reads only its diff's
  first page (see #99).

An LFS object is missing when garbage collection tombstoned it or the
bucket no longer holds it (the quick check only knows the tombstones).

The full check (``preflight``) is exact and serves the commit page. The
quick one (``quick_verdicts``) serves a commit list page: it only reports
what it can prove unavailable at bounded cost, and leaves everything else
unknown (``available: None``), never "available". Neither loads more
than ``EXACT_PATHS`` changed paths: past that, the verdict is unknown too.
"""

import asyncio
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from kohakuhub.api.operation_capabilities import get_repository_operation_capabilities
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.config import cfg
from kohakuhub.db import LFSObjectHistory, LfsHeadRef, LfsObjectTombstone, Repository
from kohakuhub.lfs_gc import deleted_shas, lfs_key, lfs_oid
from kohakuhub.logger import get_logger
from kohakuhub.utils.s3 import get_s3_client

logger = get_logger("AVAILABILITY")

PAGE = 1000  # LakeFS listing maximum
STAT_CONCURRENCY = 16
LISTING_THRESHOLD = 200  # more paths than this: list the ref instead of one stat each
EXACT_PATHS = 20_000  # changed paths an exact check reads before giving up
STORAGE_CHECK_LIMIT = 400  # LFS objects a commit's diff asks the bucket about
READER_BUDGET = 250  # LakeFS calls per unavailable-files request: 200 stats, or a listing
QUICK_BUDGET = 400  # LakeFS calls one list page may spend proving LFS objects missing
# (charged as made: a stat per path, or a page when a whole ref is listed)
SHOWN_PATHS = 20

MESSAGES = {
    "disabled": "{op} is disabled on this site.",
    "forbidden": "You need write access to this repository.",
    "initial_commit": "This is the repository's first commit; there is nothing to revert.",
    "no_changes": "{op} would not change anything.",
    "conflict": "Later commits changed the same files: {paths}.",
    "lfs_missing": "Files it needs are no longer stored (garbage collected): {paths}.",
    "already_current": "The branch is already at this commit.",
    "too_large": "{op} touches more files than can be checked in advance; it is checked when it runs.",
}


def verdict(reason: str | None = None, op: str = "", **details) -> dict[str, Any]:
    """``available`` with a reason code and a message people can read."""
    if reason is None:
        return {"available": True, **details}
    if reason == "too_large":
        return {
            "available": None,
            "reason": reason,
            "message": MESSAGES[reason].format(op=op.capitalize()),
        }
    paths = details.get("missing_lfs" if reason == "lfs_missing" else "conflicts") or []
    shown = ", ".join(paths[:SHOWN_PATHS]) + (" …" if len(paths) > SHOWN_PATHS else "")
    message = MESSAGES[reason].format(op=op.capitalize(), paths=shown)
    return {"available": False, "reason": reason, "message": message, **details}


UNKNOWN = {"available": None}


class OutOfBudget(Exception):
    """A quick check ran out of LakeFS calls, or an exact one out of paths."""


class Budget:
    """LakeFS calls a quick check may still make; charged before each one."""

    def __init__(self, calls: int):
        self.calls = calls

    def charge(self, calls: int = 1) -> None:
        if calls > self.calls:
            raise OutOfBudget
        self.calls -= calls


async def _pages(fetch, budget: Budget | None = None, limit: int | None = None) -> list[dict]:
    """Every result of a paginated listing; stops at the budget or past ``limit``."""
    results, after = [], ""
    while True:
        if budget is not None:
            budget.charge()
        page = await fetch(after)
        results += page.get("results", [])
        if limit is not None and len(results) > limit:
            raise OutOfBudget
        pagination = page.get("pagination") or {}
        if not pagination.get("has_more"):
            return results
        after = pagination["next_offset"]


async def changed_paths(
    client,
    lakefs_repo: str,
    left: str,
    right: str,
    budget: Budget | None = None,
    limit: int | None = None,
) -> list[str]:
    """Every path whose content differs between two refs (a two-dot diff)."""
    return [
        entry["path"]
        for entry in await _pages(
            lambda after: client.diff_refs(
                repository=lakefs_repo,
                left_ref=left,
                right_ref=right,
                after=after,
                amount=PAGE,
                diff_type="two_dot",
            ),
            budget,
            limit,
        )
        if entry.get("path_type", "object") == "object"
    ]


async def entries(
    client, lakefs_repo: str, ref: str, paths: list[str], budget: Budget | None = None
) -> dict[str, dict | None]:
    """``ref``'s entry at each path, ``None`` where it has none."""
    if len(paths) > LISTING_THRESHOLD:
        wanted = set(paths)
        listed = {
            obj["path"]: obj
            for obj in await _pages(
                lambda after: client.list_objects(
                    repository=lakefs_repo, ref=ref, after=after, amount=PAGE
                ),
                budget,
            )
            if obj["path"] in wanted
        }
        return {path: listed.get(path) for path in paths}
    if budget is not None:
        budget.charge(len(paths))
    limit = asyncio.Semaphore(STAT_CONCURRENCY)

    async def stat(path):
        async with limit:
            try:
                return path, await client.stat_object(repository=lakefs_repo, ref=ref, path=path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 404:
                    raise
                return path, None

    return dict(await asyncio.gather(*(stat(path) for path in paths)))


def same(a: dict | None, b: dict | None) -> bool:
    """Whether two entries hold the same content (LakeFS compares checksums)."""
    if a is None or b is None:
        return a is b
    return a["checksum"] == b["checksum"]


def _stored(s3, oid: str) -> bool:
    """Whether the bucket holds ``oid``; only "not found" counts as missing."""
    try:
        s3.head_object(Bucket=cfg.s3.bucket, Key=lfs_key(oid))
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise
        return False


async def lfs_statuses(oids, check_storage: bool = True) -> dict[str, str]:
    """Each LFS object's state: ``collected`` (garbage collection tombstoned
    it), ``missing`` (the bucket no longer holds it), ``available``, or
    ``unknown`` when the bucket was not asked."""
    oids = set(oids)
    collected = deleted_shas(oids)
    status = {oid: "collected" for oid in collected}
    rest = sorted(oids - collected)
    if not check_storage:
        return {**status, **{oid: "unknown" for oid in rest}}
    if rest:
        # One client for the batch: creating one per request costs more than the request
        s3 = await run_in_s3_executor(get_s3_client)
        limit = asyncio.Semaphore(STAT_CONCURRENCY)

        async def stored(oid):
            async with limit:
                return await run_in_s3_executor(_stored, s3, oid)

        present = await asyncio.gather(*(stored(oid) for oid in rest))
        status.update({oid: "available" if here else "missing" for oid, here in zip(rest, present)})
    return status


async def missing_lfs(needed: dict[str, dict]) -> list[str]:
    """Paths whose LFS object is gone: tombstoned, or not in the bucket."""
    oids = {path: lfs_oid(entry.get("physical_address")) for path, entry in needed.items()}
    oids = {path: oid for path, oid in oids.items() if oid}
    status = await lfs_statuses(oids.values())
    return sorted(path for path, oid in oids.items() if status[oid] != "available")


async def mark_lfs_statuses(files: list[dict]) -> None:
    """Set ``lfs_status`` / ``previous_lfs_status`` on a commit diff's files,
    from the ``_address`` / ``_previous_address`` its stats found (dropped).

    The bucket is only asked for up to ``STORAGE_CHECK_LIMIT`` objects; past
    that, anything not tombstoned is ``unknown``.
    """
    targets: dict[str, list[tuple[dict, str]]] = {}
    for file in files:  # every file, so no private key reaches the response
        for key, field in (
            ("_address", "lfs_status"),
            ("_previous_address", "previous_lfs_status"),
        ):
            oid = lfs_oid(file.pop(key, None))
            if oid:
                targets.setdefault(oid, []).append((file, field))
    try:
        status = await lfs_statuses(targets, check_storage=len(targets) <= STORAGE_CHECK_LIMIT)
    except (ClientError, BotoCoreError) as e:
        # The bucket failing must not take the diff down: tombstones only
        logger.warning(f"Could not ask the bucket which LFS objects are stored: {e}")
        status = await lfs_statuses(targets, check_storage=False)
    for oid, marks in targets.items():
        for file, field in marks:
            file[field] = status[oid]


async def unavailable_files(
    client, lakefs_repo: str, repo: Repository, commit_id: str, branch: str, head: str
) -> list | None:
    """Every LFS file of ``commit_id``'s tree that garbage collection removed.

    Collection never deletes what a branch head links (``lfs_head_ref``), so
    this is the tombstoned objects among the paths differing from ``branch``'s
    head, plus any the head itself still links (a revert, merge or reset can
    link an entry without claiming it): one diff, the tombstones, and never
    the bucket. Cheap enough for every reader, and bounded by
    ``READER_BUDGET`` LakeFS calls; ``None`` past that.
    """
    R, T = LfsHeadRef, LfsObjectTombstone
    budget = Budget(READER_BUDGET)
    try:
        paths = await changed_paths(client, lakefs_repo, head, commit_id, budget, limit=EXACT_PATHS)
        # Where the commit matches the head, only an object the head links
        # without having claimed it can be gone; confirm each at the commit.
        differing = set(paths)
        shared = {
            path
            for (path,) in R.select(R.path_in_repo)
            .join(T, on=(R.sha256 == T.sha256))
            .where((R.repository == repo) & (R.branch == branch))
            .tuples()
            if path not in differing
        }
        target = await entries(client, lakefs_repo, commit_id, paths + sorted(shared), budget)
    except OutOfBudget:
        return None
    oids = {p: lfs_oid(e.get("physical_address")) for p, e in target.items() if e is not None}
    oids = {p: oid for p, oid in oids.items() if oid}
    collected = deleted_shas(oids.values())
    return [{"path": p, "sha256": oid} for p, oid in sorted(oids.items()) if oid in collected]


def introduced_unavailable(repo: Repository, commit_ids: list[str]) -> dict[str, list[str]]:
    """The LFS files each commit committed (added, changed, restored, or
    re-committed unchanged) whose object garbage collection removed.

    One query over the LFS history, whatever the history's length.
    """
    H, T = LFSObjectHistory, LfsObjectTombstone
    found: dict[str, set[str]] = {}
    for commit_id, path in (
        H.select(H.commit_id, H.path_in_repo)
        .join(T, on=(H.sha256 == T.sha256))
        .where((H.repository == repo) & H.commit_id.in_(commit_ids))
        .tuples()
    ):
        found.setdefault(commit_id, set()).add(path)
    return {commit_id: sorted(paths) for commit_id, paths in found.items()}


async def revert_verdict(client, lakefs_repo: str, commit: dict, head: str) -> dict:
    """Exactly what reverting ``commit`` on the branch at ``head`` would do."""
    if not commit.get("parents"):
        return verdict("initial_commit", "revert")
    parent = commit["parents"][0]
    try:
        paths = await changed_paths(client, lakefs_repo, parent, commit["id"], limit=EXACT_PATHS)
    except OutOfBudget:
        return verdict("too_large", "revert")
    base, source, dest = await asyncio.gather(
        entries(client, lakefs_repo, commit["id"], paths),
        entries(client, lakefs_repo, parent, paths),
        entries(client, lakefs_repo, head, paths),
    )
    undone = [p for p in paths if same(dest[p], base[p])]
    conflicts = [p for p in paths if not same(dest[p], base[p]) and not same(dest[p], source[p])]
    # Collected versions first: the quick check can prove only that much,
    # and it holds for conflicting paths too (they differ from the branch)
    needed = {p: source[p] for p in undone + conflicts if source[p] is not None}
    missing = await missing_lfs(needed)
    if missing:
        return verdict("lfs_missing", "revert", missing_lfs=missing, conflicts=conflicts)
    if conflicts:
        return verdict("conflict", "revert", conflicts=conflicts)
    if not undone:
        # No file's content would change. LakeFS then refuses, or records an
        # empty commit where its internal layout differs: pointless either way.
        return verdict("no_changes", "revert")
    return verdict(None, files=len(undone))


async def reset_verdict(client, lakefs_repo: str, commit: dict, head: str, branch: str) -> dict:
    """Exactly what resetting the branch at ``head`` to ``commit`` would do."""
    details = {"requires_force": branch == "main"}  # the reset endpoint's own rule
    if commit["id"] == head:
        return verdict("already_current", "reset", **details)
    try:
        paths = await changed_paths(client, lakefs_repo, head, commit["id"], limit=EXACT_PATHS)
    except OutOfBudget:
        return {**verdict("too_large", "reset"), **details}
    if not paths:
        return verdict("no_changes", "reset", **details)
    target = await entries(client, lakefs_repo, commit["id"], paths)
    missing = await missing_lfs({p: e for p, e in target.items() if e is not None})
    if missing:
        return verdict("lfs_missing", "reset", missing_lfs=missing, **details)
    return verdict(None, files=len(paths), **details)


def _collected_versions(repo: Repository) -> dict[str, set[str]]:
    """Tombstoned LFS objects this repository's history references, by path."""
    H, T = LFSObjectHistory, LfsObjectTombstone
    versions: dict[str, set[str]] = {}
    for path, sha in (
        H.select(H.path_in_repo, H.sha256)
        .join(T, on=(H.sha256 == T.sha256))
        .where(H.repository == repo)
        .distinct()
        .tuples()
    ):
        versions.setdefault(path, set()).add(sha)
    return versions


async def _links_collected(client, lakefs_repo, ref, versions, budget: Budget) -> list[str]:
    """The paths where ``ref`` links a tombstoned version (raises past the budget)."""
    paths = sorted(versions)
    linked = await entries(client, lakefs_repo, ref, paths, budget)
    return [
        path
        for path, entry in linked.items()
        if entry is not None and lfs_oid(entry.get("physical_address")) in versions[path]
    ]


async def quick_verdicts(
    client, lakefs_repo: str, repo: Repository, commits: list[dict], head: str
) -> dict[str, dict]:
    """What a commit list page can show: only what is proven unavailable.

    An initial commit cannot be reverted and the head needs no reset: both
    free. LFS objects are only looked for when the repository's history
    references tombstoned ones (never, while ``lfs_auto_gc`` is off), and
    only within ``QUICK_BUDGET``:

    - resetting to ``C`` needs every object ``C`` links that the head does
      not; the head's own objects are never collected, so ``C`` linking a
      tombstoned version proves the reset impossible;
    - reverting ``C`` restores its parent's versions of the paths it
      changed; a tombstoned one among them is either needed or differs from
      the head (a conflict): impossible either way.

    Conflicts need the full check, so a revert is otherwise unknown.
    """
    versions = _collected_versions(repo)
    budget = Budget(QUICK_BUDGET)
    results = {}
    for commit in commits:
        revert, reset = UNKNOWN, UNKNOWN
        if not commit.get("parents"):
            revert = verdict("initial_commit", "revert")
        if commit["id"] == head:
            reset = verdict("already_current", "reset")
        try:
            if versions and reset is UNKNOWN:
                missing = await _links_collected(
                    client, lakefs_repo, commit["id"], versions, budget
                )
                if missing:
                    reset = verdict("lfs_missing", "reset", missing_lfs=missing)
            if versions and revert is UNKNOWN:
                # Only the paths this commit changed count
                parent = commit["parents"][0]
                changed = set(
                    await changed_paths(client, lakefs_repo, parent, commit["id"], budget)
                )
                own = {p: v for p, v in versions.items() if p in changed}
                missing = own and await _links_collected(client, lakefs_repo, parent, own, budget)
                if missing:
                    revert = verdict("lfs_missing", "revert", missing_lfs=missing)
        except OutOfBudget:
            pass  # what it has not proven stays unknown
        results[commit["id"]] = {"revert": revert, "reset": reset}
    return results


def capabilities() -> dict[str, bool]:
    enabled = get_repository_operation_capabilities()
    return {"revert": enabled["revert"], "reset": enabled["reset"]}
