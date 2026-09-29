"""Reverting a commit: a new commit undoing what it changed (#99).

LakeFS reverts natively: a three-way merge of metadata, committed atomically,
with no file content through this service, and computed on the branch's head
at that moment, so a concurrent commit needs no retry. What was missing is
checked first (``availability.revert_plan``: conflicts, versions garbage
collected, nothing to change); the versions it restores are claimed against
collection; and the commit it makes is found by a marker, never by reading
the head, then recorded like any commit (``records.record_commits``).
"""

import asyncio
import uuid

import httpx

from kohakuhub.api.commit import availability, records
from kohakuhub.api.commit.records import OperationRefused
from kohakuhub.logger import get_logger
from kohakuhub.storage_cleanup import enqueue_lfs_reconciliation

logger = get_logger("REVERT")

FIND_PAGE = 100
FIND_DEPTH = 1000  # the branch's newest commits searched for the revert's marker
NOTHING = "Nothing to revert: the branch already undoes this commit's changes."


def _conflict(conflicts: list[str], since_check: bool = False) -> OperationRefused:
    when = "the branch changed since the check, and " if since_check else ""
    if not conflicts:  # LakeFS compares object identities, the check checksums
        error = "Revert conflict: LakeFS reported a conflict with a concurrent change; try again."
    else:
        error = (
            f"Revert conflict: {when}later commits changed the same files: "
            f"{records.shown(conflicts)}"
        )
    return OperationRefused(409, {"error": error, "conflicts": conflicts})


def _refuse(plan: dict, commit_id: str) -> None:
    """Raise the answer for a revert the plan says cannot happen."""
    reason = plan["reason"]
    if reason == "initial_commit":
        raise OperationRefused(
            400, {"error": "Cannot revert the repository's first commit: it has no parent."}
        )
    if reason == "conflict":
        raise _conflict(plan["conflicts"])
    raise OperationRefused(400, {"error": NOTHING})


async def _find(client, lakefs_repo: str, branch: str, marker: str) -> str:
    """The commit carrying ``marker``: the head may already be someone else's.
    It is on the branch's first-parent chain, which merges cannot lengthen."""
    after, seen = "", 0
    while seen < FIND_DEPTH:
        page = await client.log_commits(
            repository=lakefs_repo, ref=branch, after=after, amount=FIND_PAGE, first_parent=True
        )
        for commit in page["results"]:
            if (commit.get("metadata") or {}).get("kh_operation") == marker:
                return commit["id"]
        seen += len(page["results"])
        if not page["pagination"]["has_more"]:
            break
        after = page["pagination"]["next_offset"]
    raise RuntimeError(
        f"the revert may have been applied, but its commit (marker {marker}) is not among "
        "the branch's newest commits"
    )


async def revert_commit(
    client,
    lakefs_repo: str,
    branch: str,
    commit: dict,
    parent_number: int,
    message: str,
    metadata: dict | None,
    allow_empty: bool,
) -> tuple[str, list[tuple[str, dict]]]:
    """Revert ``commit`` on ``branch`` relative to its ``parent_number``-th parent.

    Returns the new commit and ``[(commit, entries it brought)]`` for
    ``records.record_commits``. Raises ``OperationRefused``.
    """
    commit_id = commit["id"]
    parents = commit.get("parents") or []
    if parents and not 1 <= parent_number <= len(parents):
        raise OperationRefused(
            400,
            {
                "error": f"Commit {commit_id[:8]} has {len(parents)} parent(s): "
                f"parent_number must be between 1 and {len(parents)}"
            },
        )
    try:
        head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        raise OperationRefused(404, {"error": f"Branch not found: {branch}"})
    plan, restores, changed = await availability.revert_plan(
        client, lakefs_repo, commit, head, parent_number
    )
    reason = plan.get("reason")
    # Versions tombstoned are left to the claim: one uploaded again is revived
    if not plan["available"] and reason != "lfs_missing":
        if not (reason == "no_changes" and allow_empty):
            _refuse(plan, commit_id)
    # Every version it could restore is kept while it runs, also those a
    # concurrent change could make it restore: those it will are required
    await records.claim_objects(
        restores, f"revert commit {commit_id[:8]}", optional=changed.values()
    )
    if reason == "lfs_missing" and plan["conflicts"]:
        raise _conflict(plan["conflicts"])
    marker = uuid.uuid4().hex
    marked = {**(metadata or {}), "revert_of": commit_id, "kh_operation": marker}
    for attempt in range(records.DIRTY_WAITS):
        try:
            await client.revert_branch(
                repository=lakefs_repo,
                branch=branch,
                ref=commit_id,
                parent_number=parent_number,
                message=message,
                metadata=marked,
                allow_empty=allow_empty,
            )
            break
        except httpx.HTTPStatusError as e:
            why = records.refusal(e)
            if why == "conflict":  # the branch changed since the check: say where
                head = (await client.get_branch(repository=lakefs_repo, branch=branch))["commit_id"]
                again, _, _ = await availability.revert_plan(
                    client, lakefs_repo, commit, head, parent_number
                )
                raise _conflict(again.get("conflicts") or [], since_check=True)
            if why == "unchanged":
                raise OperationRefused(400, {"error": NOTHING})
            if why == "other":
                records.refused_by_lakefs(e, "revert")
            # An upload in flight on the branch: wait for it
            if attempt < records.DIRTY_WAITS - 1:
                await asyncio.sleep(records.RETRY_DELAY * (attempt + 1))
    else:
        raise OperationRefused(
            409,
            {"error": "The branch has uncommitted changes (an upload in progress?); try again."},
        )
    new = await _find(client, lakefs_repo, branch, marker)
    try:
        rounds = [await records.commit_changes(client, lakefs_repo, new)]
    except Exception as e:
        # The revert happened: record its commit, and let the reconciliation
        # record what the branch links
        logger.exception(f"Could not read what revert {new[:8]} changed", e)
        enqueue_lfs_reconciliation()
        rounds = [(new, {})]
    return new, rounds
