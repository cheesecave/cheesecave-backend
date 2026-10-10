"""A side branch's tree, paths-info, download and preupload read its own
identities, not the default branch's File rows (#11). Against the real LakeFS
and bucket; the database holds main's rows."""

from test.kohakuhub.api.commit.test_availability import Repo, _file, lfs
from test.kohakuhub.api.test_branch_reset import blob_sha1, sha
from test.kohakuhub.api.commit.test_availability import m  # noqa: F401 - the fixture


def _branch_paths(repo_row, branch):
    from kohakuhub.db import File

    return {
        f.path_in_repo
        for f in File.select().where(
            (File.repository == repo_row) & (File.branch == branch) & (File.is_deleted == False)  # noqa: E712
        )
    }


async def _tree(client, repo, ref):
    response = await client.get(f"/api/models/{repo.id}/tree/{ref}")
    assert response.status_code == 200, response.text
    return {item["path"]: item for item in response.json()}


async def test_a_side_branch_reads_its_own_identities(m, owner_client):
    repo = await Repo(m, owner_client, "side-reads").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(_file("README.md", "world"), lfs("w.bin", b"w dev"), branch="dev")

    dev = await _tree(owner_client, repo, "dev")
    assert dev["README.md"]["oid"] == blob_sha1("world")
    assert dev["w.bin"]["oid"] == sha(b"w dev")
    assert dev["w.bin"]["lfs"]["oid"] == sha(b"w dev")

    main = await _tree(owner_client, repo, "main")
    assert main["README.md"]["oid"] == blob_sha1("hello")
    assert main["w.bin"]["oid"] == sha(b"w main")

    response = await owner_client.post(
        f"/api/models/{repo.id}/paths-info/dev", data={"paths": ["README.md", "w.bin"]}
    )
    assert response.status_code == 200, response.text
    by_path = {entry["path"]: entry for entry in response.json()}
    assert by_path["README.md"]["oid"] == blob_sha1("world")
    assert by_path["w.bin"]["oid"] == sha(b"w dev")

    response = await owner_client.head(f"/models/{repo.id}/resolve/dev/README.md")
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == blob_sha1("world")
    response = await owner_client.head(f"/models/{repo.id}/resolve/dev/w.bin")
    assert response.headers["ETag"] == sha(b"w dev")

    # Main's content is on main: the side branch's upload of it is never skipped
    body = {"files": [{"path": "w.bin", "size": 6, "sha256": sha(b"w main")}]}
    response = await owner_client.post(f"/api/models/{repo.id}/preupload/dev", json=body)
    assert response.json()["files"][0]["shouldIgnore"] is False
    response = await owner_client.post(f"/api/models/{repo.id}/preupload/main", json=body)
    assert response.json()["files"][0]["shouldIgnore"] is True


async def test_a_side_branch_reads_its_rows_not_lakefs(m, owner_client):
    """The rows of the branch answer for it: a row changed in the database is what
    the tree, paths-info and download report (#11, stage 3)."""
    from kohakuhub.db import File
    from kohakuhub.db_operations import get_repository

    repo = await Repo(m, owner_client, "rows-reads").create()
    await repo.commit(lfs("w.bin", b"w main"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(lfs("w.bin", b"w dev"), branch="dev")

    row = File.get(
        (File.repository == get_repository("model", "owner", repo.name))
        & (File.branch == "dev")
        & (File.path_in_repo == "w.bin")
    )
    File.update(sha256="f" * 64).where(File.id == row.id).execute()

    dev = await _tree(owner_client, repo, "dev")
    assert dev["w.bin"]["oid"] == "f" * 64
    response = await owner_client.post(
        f"/api/models/{repo.id}/paths-info/dev", data={"paths": ["w.bin"]}
    )
    assert response.json()[0]["oid"] == "f" * 64
    response = await owner_client.head(f"/models/{repo.id}/resolve/dev/w.bin")
    assert response.headers["ETag"] == "f" * 64


async def test_a_new_branch_starts_with_its_source_rows(m, owner_client):
    """A branch made from a branch gets that branch's rows at once, before any
    commit to it (#11, stage 3)."""
    from kohakuhub.db import File
    from kohakuhub.db_operations import get_repository

    repo = await Repo(m, owner_client, "rows-copy").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    repo_row = get_repository("model", "owner", repo.name)
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text

    def rows(branch):
        return {
            f.path_in_repo: (f.sha256, f.size, f.lfs)
            for f in File.select().where(
                (File.repository == repo_row) & (File.branch == branch) & (File.is_deleted == False)  # noqa: E712
            )
        }

    assert rows("dev") == rows("main")
    assert rows("dev")["w.bin"] == (sha(b"w main"), 6, True)


async def test_a_branch_without_rows_is_read_from_lakefs_and_recorded(m, owner_client):
    """A branch that predates its rows (no row at all) answers from LakeFS once and
    records what it read (#11, stage 3): a read never shows a LakeFS checksum for an
    LFS file."""
    from kohakuhub.db import File
    from kohakuhub.db_operations import get_repository

    repo = await Repo(m, owner_client, "rows-seed").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    repo_row = get_repository("model", "owner", repo.name)
    File.delete().where((File.repository == repo_row) & (File.branch == "dev")).execute()

    dev = await _tree(owner_client, repo, "dev")
    assert dev["w.bin"]["oid"] == sha(b"w main")
    assert dev["w.bin"]["lfs"]["oid"] == sha(b"w main")
    assert dev["README.md"]["oid"] == blob_sha1("hello")
    assert File.select().where((File.repository == repo_row) & (File.branch == "dev")).count() == 2


async def test_a_commit_id_is_still_read_from_lakefs(m, owner_client):
    """A commit id is not a branch: it has no rows, so it is read from LakeFS."""
    repo = await Repo(m, owner_client, "rows-commit").create()
    first = await repo.commit(lfs("w.bin", b"w one"))
    await repo.commit(lfs("w.bin", b"w two"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/paths-info/{first}", data={"paths": ["w.bin"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["oid"] == sha(b"w one")


async def test_deleting_a_branch_over_http_drops_its_rows(m, owner_client):
    """The branch delete route takes the branch's File rows with it; main keeps
    its own (#11, stage 4)."""
    from kohakuhub.db_operations import get_repository

    repo = await Repo(m, owner_client, "drop-branch").create()
    await repo.commit(_file("README.md", "hello"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"}
    )
    assert response.status_code == 200, response.text
    await repo.commit(lfs("w.bin", b"w dev"), branch="dev")
    repo_row = get_repository("model", "owner", repo.name)
    assert _branch_paths(repo_row, "dev") == {"README.md", "w.bin"}

    response = await owner_client.delete(f"/api/models/{repo.id}/branch/dev")
    assert response.status_code == 200, response.text

    assert _branch_paths(repo_row, "dev") == set()
    assert _branch_paths(repo_row, "main") == {"README.md"}
