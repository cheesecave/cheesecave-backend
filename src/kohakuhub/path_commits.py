"""Each path's last commit on main, recorded as commits land.

A file listing's "last commit" column reads these rows instead of asking
LakeFS, whose path-filtered log costs a check of nearly every commit of the
history (see LAST_COMMIT_LOOKUP_CONCURRENCY in api.repo.routers.tree). A
commit records the paths it changed and every folder above them, so a
folder's last commit is the last one that changed anything under it,
deletions included, as on the Hub.

Only main is recorded; other revisions are looked up the old, bounded way. A
repository from before the rows is recorded by ``backfill``.
"""

import asyncio
from typing import Any, Iterable

from peewee import EXCLUDED

from kohakuhub import tasks, usage
from kohakuhub.db import PathCommit, Repository
from kohakuhub.lakefs_rest_client import get_lakefs_rest_client
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import resolve_lakefs_repo
from kohakuhub.api.repo.utils.hf import is_lakefs_not_found_error

logger = get_logger("PATH_COMMITS")

BRANCH = usage.MAIN
BACKFILL_KIND = "last_commits.backfill"
ROW_BATCH = 500
LAKEFS_PAGE = 1000


def with_folders(paths: Iterable[str]) -> set[str]:
    """``paths`` and every folder above them."""
    out = set()
    for path in paths:
        parts = list(filter(None, path.split("/")))
        for depth in range(1, len(parts) + 1):
            out.add("/".join(parts[:depth]))
    return out


def _rows(repo: Repository, commit: dict, paths: Iterable[str]) -> list[dict]:
    return [
        {
            "repository": repo,
            "branch": BRANCH,
            "path": path,
            "commit_id": commit["id"],
            "title": commit.get("message", ""),
            "date": int(commit.get("creation_date") or 0),
        }
        for path in sorted(with_folders(paths))
    ]


async def _write(rows: list[dict], replace: bool) -> None:
    """Batch by batch, letting the event loop run between them: a commit
    may change hundreds of thousands of paths."""
    P = PathCommit
    target = (P.repository, P.branch, P.path)
    for start in range(0, len(rows), ROW_BATCH):
        query = P.insert_many(rows[start : start + ROW_BATCH])
        if replace:  # never by an older commit: two commits may record out of order
            query = query.on_conflict(
                conflict_target=target,
                update={P.commit_id: EXCLUDED.commit_id, P.title: EXCLUDED.title, P.date: EXCLUDED.date},
                where=(P.date <= EXCLUDED.date),
            )
        else:
            query = query.on_conflict_ignore()
        query.execute()
        await asyncio.sleep(0)


async def record(repo: Repository, branch: str, commit: dict, paths: Iterable[str]) -> None:
    """Record ``commit`` (a LakeFS commit) as the last commit of ``paths``
    and the folders above them, on main."""
    if branch == BRANCH:
        await _write(_rows(repo, commit, paths), replace=True)


def record_squash(repo: Repository, branch: str, commit: dict) -> None:
    """A squash's commit is the last commit of everything on the branch."""
    if branch == BRANCH:
        P = PathCommit
        P.update(
            commit_id=commit["id"],
            title=commit.get("message", ""),
            date=int(commit.get("creation_date") or 0),
        ).where((P.repository == repo) & (P.branch == BRANCH)).execute()


def recorded(repo: Repository, revision: str, paths: list[str]) -> dict[str, dict]:
    """The recorded last commits of ``paths`` at ``revision``, shaped like a
    LakeFS commit; a path without one is left out."""
    if revision != BRANCH or not paths:
        return {}
    P = PathCommit
    found = {}
    for start in range(0, len(paths), ROW_BATCH):
        for row in P.select().where(
            (P.repository == repo)
            & (P.branch == BRANCH)
            & P.path.in_(paths[start : start + ROW_BATCH])
        ):
            found[row.path] = {"id": row.commit_id, "message": row.title, "creation_date": row.date}
    return found


async def _pages(fetch):
    after = ""
    while True:
        page = await fetch(after)
        yield page.get("results") or []
        pagination = page.get("pagination") or {}
        if not pagination.get("has_more"):
            return
        after = pagination.get("next_offset") or ""


async def _changed(client, lakefs_repo: str, commit: dict) -> list[str]:
    """The paths ``commit`` changed from its first parent; for a parentless
    one (a squash, the empty first commit), everything it holds."""
    parents = commit.get("parents") or []
    if parents:
        fetch = lambda after: client.diff_refs(  # noqa: E731
            repository=lakefs_repo, left_ref=parents[0], right_ref=commit["id"],
            after=after or None, amount=LAKEFS_PAGE, diff_type="two_dot",
        )
    else:
        fetch = lambda after: client.list_objects(  # noqa: E731
            repository=lakefs_repo, ref=commit["id"], after=after, amount=LAKEFS_PAGE
        )
    return [entry["path"] async for page in _pages(fetch) for entry in page]


async def backfill_repository(client, repo: Repository) -> int:
    """Record main's last commits for a repository from before the rows;
    the commits read.

    Main's history is read newest first and a path keeps the first commit
    seen changing it, its last; a row already there (a commit landing
    meanwhile) is newer and stays. ponytail: a squash during it can leave a
    path with a commit the squash dropped, until that path changes again.
    """
    lakefs_repo = resolve_lakefs_repo(repo)
    seen = 0
    log = lambda after: client.log_commits(  # noqa: E731
        repository=lakefs_repo, ref=BRANCH, after=after or None, amount=LAKEFS_PAGE, first_parent=True
    )
    async for commits in _pages(log):
        for commit in commits:
            await _write(_rows(repo, commit, await _changed(client, lakefs_repo, commit)), replace=False)
            seen += 1
    return seen


@tasks.task(BACKFILL_KIND, timeout=24 * 3600, max_attempts=5)
async def backfill(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Record every repository from before the rows, one at a time; each is
    marked once done, so a retry resumes with the next."""
    client = get_lakefs_rest_client()
    R = Repository
    pending = R.select().where(R.last_commits_recorded == False)  # noqa: E712
    total, done = pending.count(), 0
    for repo in pending.order_by(R.id):
        ctx.stage(f"recording {repo.full_id}")
        try:
            commits = await backfill_repository(client, repo)
        except Exception as error:
            if not is_lakefs_not_found_error(error):
                raise
            commits = 0  # its LakeFS repository is gone: nothing to record
        R.update(last_commits_recorded=True).where(R.id == repo.id).execute()
        done += 1
        ctx.progress(done, total)
        logger.info(f"Recorded the last commits of {repo.full_id} ({commits} commit(s))")


def ensure_backfill() -> int | None:
    """Queue the backfill while a repository is not recorded; ``None`` if
    none is, or one is queued already."""
    R = Repository
    if not R.select().where(R.last_commits_recorded == False).exists():  # noqa: E712
        return None
    return tasks.enqueue(BACKFILL_KIND, dedupe_key=BACKFILL_KIND)
