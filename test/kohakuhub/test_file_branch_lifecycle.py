"""A branch's File rows leave with the branch; a repository's usage counts each
LFS object once; the LFS collection and the squash read their own branch's rows
(#11, stage 4). The database is real; LakeFS is a fake."""

from datetime import datetime, timedelta, timezone

import pytest

from kohakuhub import storage_cleanup
from kohakuhub.api.commit.routers.operations import calculate_git_blob_sha1 as _blob
from kohakuhub.db import File, LFSObjectHistory, Repository
from kohakuhub.db_operations import delete_repository, repository_lfs_totals
from kohakuhub.lfs_gc import reconcile_references, retention_reason
from kohakuhub.storage_cleanup import drop_branch_rows, forget_branch
from test.kohakuhub.support.factories import make_file, make_repo, make_user

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_M = "m" * 64
SHA_D = "d" * 64


def _paths(repo, branch):
    return {
        f.path_in_repo
        for f in File.select().where(
            (File.repository == repo) & (File.branch == branch) & (File.is_deleted == False)  # noqa: E712
        )
    }


@pytest.mark.usefixtures("db_scope")
def test_dropping_a_branch_removes_only_its_rows():
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", SHA_M, branch="main")
    make_file(repo, "README.md", SHA_D, branch="dev")
    make_file(repo, "weights.bin", SHA_A, lfs=True, branch="dev")
    make_file(repo, "README.md", SHA_B, branch="other")

    assert drop_branch_rows(repo, "dev") == 2
    assert _paths(repo, "dev") == set()
    assert _paths(repo, "main") == {"README.md"}
    assert _paths(repo, "other") == {"README.md"}


@pytest.mark.usefixtures("db_scope")
def test_deleting_a_repository_removes_the_rows_of_every_branch():
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", SHA_M, branch="main")
    make_file(repo, "README.md", SHA_D, branch="dev")
    repo_id = repo.id

    delete_repository(repo)

    assert File.select().where(File.repository == repo_id).count() == 0
    assert Repository.get_or_none(Repository.id == repo_id) is None


@pytest.mark.usefixtures("db_scope")
def test_forgetting_a_branch_drops_its_rows_and_keeps_main():
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "README.md", SHA_M, branch="main")
    make_file(repo, "README.md", SHA_D, branch="dev")

    forget_branch(repo, "dev")

    assert _paths(repo, "dev") == set()
    assert _paths(repo, "main") == {"README.md"}


@pytest.mark.usefixtures("db_scope")
def test_lfs_totals_count_each_object_once_over_every_branch():
    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "a.bin", SHA_A, size=10, lfs=True, branch="main")
    make_file(repo, "a.bin", SHA_A, size=10, lfs=True, branch="dev")  # same object
    make_file(repo, "b.bin", SHA_B, size=5, lfs=True, branch="dev")
    make_file(repo, "c.txt", SHA_D, size=99, lfs=False, branch="dev")  # regular: not LFS
    make_file(repo, "gone.bin", SHA_M, size=7, lfs=True, branch="dev", is_deleted=True)

    assert repository_lfs_totals(repo) == (15, 2)


@pytest.mark.usefixtures("db_scope")
def test_retention_keeps_an_object_an_active_side_branch_row_links():
    repo = make_repo(make_user("owner"), "repo")
    row = make_file(repo, "w.bin", SHA_A, size=10, lfs=True, branch="dev")

    assert retention_reason(SHA_A) == "file"
    File.update(is_deleted=True).where(File.id == row.id).execute()
    assert retention_reason(SHA_A) is None


@pytest.mark.usefixtures("db_scope")
def test_reconciliation_leaves_a_side_branch_row_alone():
    repo = make_repo(make_user("owner"), "repo")
    side = make_file(repo, "w.bin", SHA_D, size=10, lfs=True, branch="dev")

    stats = reconcile_references(repo, set(), {"w.bin": (SHA_M, 10)}, ["w.bin"])

    assert stats["files_fixed"] == 1
    assert File.get_by_id(side.id).sha256 == SHA_D
    main = File.get((File.repository == repo) & (File.branch == "main") & (File.path_in_repo == "w.bin"))
    assert (main.sha256, main.size, main.lfs) == (SHA_M, 10, True)


@pytest.mark.usefixtures("db_scope")
def test_the_history_file_link_is_nulled_when_its_row_goes_and_the_history_stays():
    repo = make_repo(make_user("owner"), "repo")
    row = make_file(repo, "w.bin", SHA_A, size=10, lfs=True, branch="dev")
    history = LFSObjectHistory.create(
        repository=repo, path_in_repo="w.bin", sha256=SHA_A, size=10, commit_id="c1", file=row
    )

    drop_branch_rows(repo, "dev")

    kept = LFSObjectHistory.get_by_id(history.id)
    assert kept.file_id is None
    assert kept.sha256 == SHA_A


@pytest.mark.usefixtures("db_scope")
async def test_squash_of_main_leaves_other_branches_rows_alone(monkeypatch):
    repo = make_repo(make_user("owner"), "repo")
    old = datetime.now(timezone.utc) - timedelta(days=1)
    for branch in ("main", "dev"):
        make_file(repo, "keep.txt", SHA_M, branch=branch)
    make_file(repo, "gone.txt", SHA_M, branch="main")
    dev_only = make_file(repo, "dev-only.txt", SHA_D, branch="dev")
    File.update(updated_at=old).execute()

    class _Client:
        async def get_repository(self, repository):
            return {"storage_namespace": "s3://elsewhere/prefix"}

    async def tree(client, lakefs_repo, ref):
        return [{"path": "keep.txt", "physical_address": ""}]

    monkeypatch.setattr(storage_cleanup, "get_lakefs_client", lambda: _Client())
    monkeypatch.setattr(storage_cleanup, "resolve_lakefs_repo", lambda repo: "lake")
    monkeypatch.setattr(storage_cleanup, "_tree", tree)

    await (
        storage_cleanup.forget_squashed_history(
            {
                "repo_id": repo.id,
                "branch": "main",
                "commit": "squash",
                "through": 0,
                "at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
        )
    )

    assert _paths(repo, "main") == {"keep.txt"}
    assert _paths(repo, "dev") == {"keep.txt", "dev-only.txt"}
    assert File.get_by_id(dev_only.id).is_deleted is False


@pytest.mark.usefixtures("db_scope")
async def test_admin_storage_counts_the_default_branch_files_and_each_lfs_object_once():
    from kohakuhub.api.admin.routers.repositories import get_repository_storage_breakdown

    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "a.bin", SHA_A, size=10, lfs=True, branch="main")
    make_file(repo, "readme.md", SHA_M, size=3, branch="main")
    make_file(repo, "a.bin", SHA_A, size=10, lfs=True, branch="dev")  # same object
    make_file(repo, "b.bin", SHA_B, size=5, lfs=True, branch="dev")
    make_file(repo, "side.md", SHA_D, size=4, branch="dev")

    breakdown = await get_repository_storage_breakdown("model", "owner", "repo", _admin=True)

    assert breakdown["regular_files_size"] == 3  # main's regular files only
    assert breakdown["lfs_files_size"] == 15  # a.bin once, b.bin once
    assert breakdown["unique_lfs_objects"] == 2
    assert breakdown["lfs_object_count"] == 3  # the LFS rows of every branch




@pytest.mark.usefixtures("db_scope")
async def test_top_repositories_by_size_count_the_default_branch_only():
    from kohakuhub.api.admin.routers.stats import get_top_repositories

    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "a.bin", SHA_A, size=10, lfs=True, branch="main")
    make_file(repo, "a.bin", SHA_A, size=500, lfs=True, branch="dev")

    listing = await get_top_repositories(limit=10, by="size", _admin=True)
    [top] = [r for r in listing["top_repositories"] if r["repo_full_id"] == "owner/repo"]
    assert top["total_size"] == 10  # main's a.bin only, not the 500 bytes on dev



class _Lake:
    """LakeFS (a fake): branches, pages of a listing, and the bytes of regular files."""

    def __init__(self, pages=None, branches=None, contents=None):
        self.pages = list(pages or [])
        self.branches = dict(branches or {})
        self.contents = dict(contents or {})
        self.listed = 0

    async def get_branch(self, repository, branch):
        if isinstance(self.branches.get(branch), Exception):
            raise self.branches[branch]
        if branch not in self.branches:
            raise RuntimeError("404 not found: branch")
        return {"commit_id": self.branches[branch]}

    async def list_objects(self, **kwargs):
        self.listed += 1
        return self.pages.pop(0)

    async def get_object(self, repository, ref, path):
        return self.contents[path]


def _entry(path, content):
    return {
        "path": path,
        "path_type": "object",
        "size_bytes": len(content),
        "checksum": "sha256:x",
        "physical_address": f"s3://bucket/data/{path}",
    }


@pytest.mark.usefixtures("db_scope")
async def test_seeding_a_branch_reads_every_page_of_its_listing():
    from kohakuhub.api.commit import records

    repo = make_repo(make_user("owner"), "repo")
    lake = _Lake(
        pages=[
            {"results": [_entry("a.md", b"aa")], "pagination": {"has_more": True, "next_offset": "a.md"}},
            {"results": [_entry("b.md", b"bb")], "pagination": {"has_more": False}},
        ],
        contents={"a.md": b"aa", "b.md": b"bb"},
    )

    await records._seed_rows(lake, "lake", repo, "dev", "c1")

    assert lake.listed == 2
    assert _paths(repo, "dev") == {"a.md", "b.md"}
    assert File.get(
        (File.repository == repo) & (File.branch == "dev") & (File.path_in_repo == "b.md")
    ).sha256 == _blob(b"bb")


@pytest.mark.usefixtures("db_scope")
async def test_a_lakefs_error_that_is_not_a_missing_branch_is_raised():
    from kohakuhub.api.commit import records

    lake = _Lake(branches={"dev": RuntimeError("connection reset")})
    with pytest.raises(RuntimeError, match="connection reset"):
        await records.revision_branch(lake, "lake", "dev")
    assert await records.revision_branch(lake, "lake", "absent") is None


@pytest.mark.usefixtures("db_scope")
async def test_a_branch_whose_rows_appear_while_it_waits_is_not_read_again(monkeypatch):
    from kohakuhub.api.commit import records

    repo = make_repo(make_user("owner"), "repo")
    lake = _Lake(branches={"dev": "c1"})
    answers = iter([False, True])  # no rows when looked at first, then another operation's
    monkeypatch.setattr(records, "_has_rows", lambda repo, branch: next(answers))

    assert await records.read_branch(lake, "lake", repo, "dev") == "dev"
    assert lake.listed == 0


@pytest.mark.usefixtures("db_scope")
async def test_admin_file_listing_shows_the_branch_its_ref_names(monkeypatch):
    from kohakuhub.api.admin.routers import repositories as admin

    repo = make_repo(make_user("owner"), "repo")
    make_file(repo, "w.bin", SHA_M, size=10, lfs=True, branch="main")
    make_file(repo, "w.bin", SHA_D, size=10, lfs=True, branch="dev")
    lake = _Lake(
        branches={"dev": "c1"},
        pages=[{"results": [{"path": "w.bin", "size_bytes": 10, "checksum": "x"}]}],
    )
    monkeypatch.setattr(admin, "get_lakefs_client", lambda: lake)
    monkeypatch.setattr(admin, "resolve_lakefs_repo", lambda repo: "lake")

    listing = await admin.get_repository_files_admin("model", "owner", "repo", "dev", _admin=True)

    [entry] = listing["files"]
    assert (entry["sha256"], entry["version_count"]) == (SHA_D, 1)
