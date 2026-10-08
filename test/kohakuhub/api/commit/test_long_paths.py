"""Paths longer than 255 characters, and a commit that fails part way.

2026-10-05 (cheesecave-backend#1): a 265-character path in one repository
stopped the Last Commit backfill there for every repository after it. The
path had got in through a commit that failed: the File row did not fit
VARCHAR(255), the request returned 500, and what it had staged in LakeFS
went into the next commit on the branch.
"""

import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _delete, _file, _live, m  # noqa: F401
from test.kohakuhub.api.helpers import encode_ndjson

# The production path: 265 characters, a 254-character name
LONG = "_pathtest3/" + "x" * 250 + ".txt"
# 308 characters, 912 bytes: under the byte limit, far over 255 characters
WIDE = "日本語/" + "長" * 300 + ".txt"
TOO_LONG = "deep/" + "y" * 1020  # 1025 bytes


@pytest.fixture
def lakefs(m):
    return _live("kohakuhub.lakefs_rest_client").get_lakefs_rest_client()


def _lakefs_repo(m, repo):
    return m.lakefs.resolve_lakefs_repo(m.db.Repository.get(m.db.Repository.full_id == repo.id))


async def _staged(m, lakefs, repo, branch="main"):
    """The branch's uncommitted changes."""
    response = await lakefs._httpx().get(
        f"{lakefs.base_url}/repositories/{_lakefs_repo(m, repo)}/branches/{branch}/diff",
        auth=lakefs.auth,
    )
    response.raise_for_status()
    return sorted(e["path"] for e in response.json()["results"])


async def _changed(m, lakefs, repo, commit):
    diff = await lakefs.diff_refs(
        repository=_lakefs_repo(m, repo), left_ref=f"{commit}~1", right_ref=commit
    )
    return sorted(e["path"] for e in diff["results"])


def _files(m, repo):
    F = m.db.File
    row = m.db.Repository.get(m.db.Repository.full_id == repo.id)
    return {
        f.path_in_repo: (f.sha256, f.size, f.is_deleted)
        for f in F.select().where(F.repository == row)
    }


async def _post(repo, *ops, summary="change"):
    return await repo.client.post(
        f"/api/models/{repo.id}/commit/main",
        content=encode_ndjson([{"key": "header", "value": {"summary": summary}}, *ops]),
        headers={"Content-Type": "application/x-ndjson"},
    )


async def test_a_path_over_255_characters_is_committed_and_listed(m, owner_client, lakefs):
    repo = await Repo(m, owner_client, "lp-commit").create()
    commit = await repo.commit(_file(LONG, "long"), _file(WIDE, "wide"), summary="long paths")

    assert set(_files(m, repo)) == {LONG, WIDE}
    assert await _changed(m, lakefs, repo, commit) == sorted([LONG, WIDE])
    response = await owner_client.post(
        f"/api/models/{repo.id}/paths-info/main",
        data={"paths": ["_pathtest3", LONG, WIDE], "expand": "true"},
    )
    assert response.status_code == 200, response.text
    assert {e["path"]: e["lastCommit"]["id"] for e in response.json()} == {
        "_pathtest3": commit,
        LONG: commit,
        WIDE: commit,
    }


async def test_a_path_over_the_limit_is_refused_before_anything_is_staged(m, owner_client, lakefs):
    repo = await Repo(m, owner_client, "lp-refused").create()
    await repo.commit(_file("base.txt", "b"), summary="base")
    files = _files(m, repo)

    response = await _post(repo, _file("first.txt", "f"), _file(TOO_LONG, "t"))

    assert response.status_code == 400, response.text
    assert response.headers["X-Error-Code"] == "BadRequest"
    assert "1024 bytes" in response.headers["X-Error-Message"]
    assert await _staged(m, lakefs, repo) == []  # not even the path before it
    assert _files(m, repo) == files
    after = await repo.commit(_file("next.txt", "n"), summary="next")
    assert await _changed(m, lakefs, repo, after) == ["next.txt"]


@pytest.mark.parametrize("failure", ["error", "refusal"])
async def test_a_failed_commit_leaves_nothing_for_the_next_one(
    m, owner_client, lakefs, monkeypatch, failure
):
    repo = await Repo(m, owner_client, f"lp-failed-{failure}").create()
    await repo.commit(
        _file("a.txt", "1"), _file("gone.txt", "g"), _file("keep/x.txt", "x"), summary="base"
    )
    files = _files(m, repo)
    operations = _live("kohakuhub.api.commit.routers.operations")
    process = operations.process_regular_file

    # Deliberate database failure, kept as a targeted mock: a real table outage
    # would fail the earlier writes of this same commit before boom.txt is reached.
    async def failing(**kwargs):
        if kwargs["path"] == "boom.txt":
            if failure == "error":
                raise RuntimeError("database is gone")
            raise operations.HTTPException(400, detail={"error": "refused"})
        return await process(**kwargs)

    monkeypatch.setattr(operations, "process_regular_file", failing)
    response = await _post(
        repo,
        _file("a.txt", "2"),
        _file("new.txt", "n"),
        _delete("gone.txt"),
        {"key": "deletedFolder", "value": {"path": "keep"}},
        _file("boom.txt", "b"),
    )

    assert response.status_code == (500 if failure == "error" else 400), response.text
    assert await _staged(m, lakefs, repo) == []
    assert _files(m, repo) == files  # rows back as they were: new.txt gone again
    monkeypatch.setattr(operations, "process_regular_file", process)
    after = await repo.commit(_file("next.txt", "n"), summary="next")
    assert await _changed(m, lakefs, repo, after) == ["next.txt"]


async def test_cleaning_up_after_a_failure_is_best_effort(m, owner_client, lakefs, monkeypatch):
    """A cleanup that fails itself does not hide what failed first."""
    repo = await Repo(m, owner_client, "lp-cleanup").create()
    operations = _live("kohakuhub.api.commit.routers.operations")
    process = operations.process_regular_file

    async def failing(**kwargs):
        if kwargs["path"] == "boom.txt":
            raise RuntimeError("database is gone")
        return await process(**kwargs)

    async def broken_reset(**kwargs):
        raise RuntimeError("LakeFS is gone too")

    # Deliberate database outage for the cleanup only, kept as a targeted mock:
    # a real table outage would fail the staging write before the cleanup runs.
    class BrokenDatabase:
        def atomic(self):
            raise RuntimeError("the database is gone too")

    monkeypatch.setattr(operations, "process_regular_file", failing)
    monkeypatch.setattr(operations, "db", BrokenDatabase())
    monkeypatch.setattr(type(lakefs), "reset_uncommitted", lambda self, **kw: broken_reset(**kw))
    response = await _post(repo, _file("staged.txt", "s"), _file("boom.txt", "b"))

    assert response.status_code == 500
    assert "database is gone" in response.text


async def test_an_operation_without_a_path_fails_cleanly(m, owner_client, lakefs):
    repo = await Repo(m, owner_client, "lp-no-path").create()
    response = await _post(repo, _file("staged.txt", "s"), {"key": "file", "value": {}})

    assert response.status_code >= 400
    assert await _staged(m, lakefs, repo) == []


@pytest.mark.parametrize("answer", ["locked", "refused", "unknown"])
async def test_a_commit_that_does_not_land(m, owner_client, lakefs, monkeypatch, answer):
    """Refused, nothing was committed: undo. No answer, the commit may
    have landed: leave the branch as it is."""
    repo = await Repo(m, owner_client, f"lp-commit-{answer}").create()
    await repo.commit(_file("a.txt", "1"), summary="base")
    files = _files(m, repo)
    operations = _live("kohakuhub.api.commit.routers.operations")
    httpx = _live("httpx")

    async def refused(self, **kwargs):
        request = httpx.Request("POST", "http://lakefs/commits")
        raise httpx.HTTPStatusError(
            "conflict", request=request, response=httpx.Response(409, request=request)
        )

    async def unknown(self, **kwargs):
        raise httpx.ConnectError("connection dropped")

    if answer == "locked":
        # The lock is a real row, taken by another operation. The pre-check is
        # passed so the refusal comes from the write itself, after staging, and
        # the wait is shortened so it comes quickly.
        row = m.db.Repository.get(m.db.Repository.full_id == repo.id)
        operations.operation_lock.acquire(row.id, "squash")
        monkeypatch.setattr(operations.operation_lock, "ensure_free", lambda repo_row: None)
        monkeypatch.setattr(operations.operation_lock, "WAIT_SECONDS", 0.2)
    else:
        monkeypatch.setattr(type(lakefs), "commit", refused if answer == "refused" else unknown)
    response = await _post(repo, _file("a.txt", "2"), _file("new.txt", "n"))

    assert response.status_code == (409 if answer == "locked" else 500), response.text
    if answer == "unknown":
        assert await _staged(m, lakefs, repo) == ["a.txt", "new.txt"]
        assert set(_files(m, repo)) == {"a.txt", "new.txt"}
    else:
        assert await _staged(m, lakefs, repo) == []
        assert _files(m, repo) == files
