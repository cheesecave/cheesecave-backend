"""Defects found by the production-shaped rollout rehearsal (issue #11, full A).

The database and LakeFS are the real ones. The pre-A key
``file_repository_id_path_in_repo`` is put back for the test, because a fresh
schema no longer has it: that is the state of an expanded database before the
contract (migration 033).
"""

import hashlib
from contextlib import contextmanager

import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _file, lfs  # noqa: F401
from test.kohakuhub.api.commit.test_availability import m  # noqa: F401 - the fixture

PRE_A_KEY = "file_repository_id_path_in_repo"


@contextmanager
def pre_a_key(m):
    """Put the pre-A key back. Other tests leave non-main rows behind, and a unique
    index cannot be built over those, so the non-main rows of the shared database go
    first (a test never reads another test's rows)."""
    db = m.db.db
    db.execute_sql("DELETE FROM \"file\" WHERE \"branch\" <> 'main'")
    db.execute_sql(f'CREATE UNIQUE INDEX "{PRE_A_KEY}" ON "file" ("repository_id", "path_in_repo")')
    try:
        yield
    finally:
        db.execute_sql(f'DROP INDEX IF EXISTS "{PRE_A_KEY}"')


def blob(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _tree(client, repo, ref):
    response = await client.get(f"/api/models/{repo.id}/tree/{ref}")
    assert response.status_code == 200, response.text
    return {item["path"]: item["oid"] for item in response.json()}


async def _branch(client, repo, name, revision="main"):
    response = await client.post(
        f"/api/models/{repo.id}/branch", json={"branch": name, "revision": revision}
    )
    assert response.status_code == 200, response.text
    return response


async def _head(client, repo, ref):
    return (await client.get(f"/api/models/{repo.id}/revision/{ref}")).json()["sha"]


def _rows(repo_row, branch):
    from kohakuhub.db import File

    return {
        f.path_in_repo: (f.sha256, f.size, f.lfs)
        for f in File.select().where(
            (File.repository == repo_row) & (File.branch == branch) & (File.is_deleted == False)  # noqa: E712
        )
    }


def _repo_row(repo):
    from kohakuhub.db_operations import get_repository

    return get_repository("model", "owner", repo.name)


async def _post_commit(client, repo, branch, *lines):
    import json

    body = "\n".join(json.dumps(line) for line in lines)
    return await client.post(
        f"/api/models/{repo.id}/commit/{branch}",
        content=body,
        headers={"Content-Type": "application/x-ndjson"},
    )


def _header(summary="change"):
    return {"key": "header", "value": {"summary": summary}}


def _text_file(path, text):
    import base64

    return {
        "key": "file",
        "value": {
            "path": path,
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
        },
    }


# --- D2: a refused side-branch commit is a 409 with a clear body --------------------


async def test_d2_side_commit_refused_by_pre_a_key_is_409(m, owner_client):
    """A side-branch commit touching a path main has: 409 naming the branch and the
    path, nothing written, LakeFS as it was."""
    repo = await Repo(m, owner_client, "d2-refused").create()
    await repo.commit(_file("README.md", "hello"))
    await _branch(owner_client, repo, "dev")  # before the pre-A key: the copy fits
    repo_row = _repo_row(repo)
    from kohakuhub.db import File

    File.delete().where((File.repository == repo_row) & (File.branch == "dev")).execute()

    with pre_a_key(m):
        head_before = await _head(owner_client, repo, "dev")
        response = await _post_commit(
            owner_client, repo, "dev", _header("refused"), _text_file("README.md", "world")
        )
        assert response.status_code == 409, response.text
        error = response.json()["detail"]["error"]
        assert "'dev'" in error and "'README.md'" in error
        assert "contract" in error and "new path" in error
        assert "duplicate key" not in response.text.lower()
        assert response.headers["X-Error-Code"] == "BranchPathConflict"
        assert _rows(repo_row, "dev") == {}
        assert await _head(owner_client, repo, "dev") == head_before
        assert await _tree(owner_client, repo, "dev") == {"README.md": blob("hello")}


async def test_d2_a_new_path_on_a_side_branch_still_commits_during_the_window(m, owner_client):
    """The refusal is for a path main has; a path main lacks commits normally."""
    repo = await Repo(m, owner_client, "d2-new-path").create()
    await repo.commit(_file("README.md", "hello"))
    await _branch(owner_client, repo, "dev")
    with pre_a_key(m):
        response = await _post_commit(
            owner_client, repo, "dev", _header(), _text_file("notes.txt", "dev only")
        )
        assert response.status_code == 200, response.text
        assert (await _tree(owner_client, repo, "dev"))["notes.txt"] == blob("dev only")


# --- D3: branch from a populated main -----------------------------------------------


async def test_d3_branch_from_populated_main_copies_exactly_the_source_rows(m, owner_client):
    repo = await Repo(m, owner_client, "d3-populated").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    await _branch(owner_client, repo, "dev")
    repo_row = _repo_row(repo)
    assert _rows(repo_row, "dev") == _rows(repo_row, "main")
    assert len(_rows(repo_row, "dev")) == 2


async def test_d3_branch_from_populated_main_during_the_window_is_200_without_an_orphan(m, owner_client):
    """Before the contract the old key refuses the copy of paths main has. The branch
    is still created (200): it has no rows and reads them from LakeFS."""
    repo = await Repo(m, owner_client, "d3-window").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    with pre_a_key(m):
        await _branch(owner_client, repo, "dev")
        assert _rows(_repo_row(repo), "dev") == {}
        assert await _tree(owner_client, repo, "dev") == {
            "README.md": blob("hello"),
            "w.bin": sha(b"w main"),
        }


@pytest.fixture
def failing_copy(m):
    """A copy that fails for a reason other than the old key (a PL/pgSQL trigger on
    the new branch's rows). PostgreSQL only: the trigger is its language."""
    if m.cfg.app.db_backend != "postgres":
        pytest.skip("the forced failure is a PL/pgSQL trigger")
    db = m.db.db
    db.execute_sql(
        "CREATE OR REPLACE FUNCTION d3_refuse_copy() RETURNS trigger AS $$ BEGIN "
        "IF NEW.branch = 'dev-copy' THEN RAISE EXCEPTION 'forced copy failure' USING ERRCODE = 'XX000'; "
        "END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
    )
    db.execute_sql(
        "CREATE TRIGGER d3_refuse_copy BEFORE INSERT ON \"file\" FOR EACH ROW "
        "EXECUTE FUNCTION d3_refuse_copy()"
    )
    yield db
    db.execute_sql("DROP TRIGGER IF EXISTS d3_refuse_copy ON \"file\"")
    db.execute_sql("DROP FUNCTION IF EXISTS d3_refuse_copy()")


async def test_d3_a_copy_failure_whose_branch_cannot_be_removed_still_answers_500(m, owner_client, failing_copy, monkeypatch):
    """The compensation is best effort: when LakeFS cannot remove the branch, the
    client still gets the failure, and the leftover is logged (not hidden)."""
    from kohakuhub.utils.lakefs import get_lakefs_client

    repo = await Repo(m, owner_client, "d3-stuck").create()
    await repo.commit(_file("README.md", "hello"))

    async def refuse_delete(**kwargs):
        raise RuntimeError("LakeFS is unreachable")

    monkeypatch.setattr(get_lakefs_client(), "delete_branch", refuse_delete)
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev-copy", "revision": "main"}
    )
    assert response.status_code == 500, response.text
    assert "forced copy failure" in response.text


async def test_d3_forced_copy_failure_leaves_no_lakefs_branch_and_no_rows(m, owner_client, failing_copy):
    """A copy that fails for another reason (not the old key) deletes the LakeFS branch
    it just made: a retry must not meet 409 on an orphan."""
    from kohakuhub import lakefs_rest_client

    repo = await Repo(m, owner_client, "d3-forced").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    response = await owner_client.post(
        f"/api/models/{repo.id}/branch", json={"branch": "dev-copy", "revision": "main"}
    )
    assert response.status_code == 500, response.text
    assert "forced copy failure" in response.text
    client = m.lakefs.get_lakefs_client()
    with pytest.raises(Exception, match="404|not found"):
        await client.get_branch(repository=repo.lakefs_repo, branch="dev-copy")
    assert _rows(_repo_row(repo), "dev-copy") == {}
    # The retry is a fresh attempt, not a 409 on an orphan
    failing_copy.execute_sql("DROP TRIGGER d3_refuse_copy ON \"file\"")
    await _branch(owner_client, repo, "dev-copy")
    lakefs_rest_client._singleton_client = None


# --- D4: a side-branch tree never answers with a LakeFS checksum --------------------


async def test_d4_a_row_less_side_branch_path_reports_the_git_blob_id(m, owner_client):
    """A side branch whose rows are missing for a path main has (a refused commit and
    the window's pre-A key) reports that path's git blob id, not LakeFS's checksum."""
    repo = await Repo(m, owner_client, "d4-tree").create()
    await repo.commit(_file("README.md", "hello"), lfs("w.bin", b"w main"))
    await _branch(owner_client, repo, "dev")
    from kohakuhub.db import File

    File.delete().where((File.repository == _repo_row(repo)) & (File.branch == "dev")).execute()

    with pre_a_key(m):
        response = await _post_commit(owner_client, repo, "dev", _header(), _text_file("README.md", "world"))
        assert response.status_code != 200, response.text  # refused (D2)
        tree = await _tree(owner_client, repo, "dev")
        assert tree["README.md"] == blob("hello")
        assert tree["w.bin"] == sha(b"w main")
        # The download and paths-info answer the same ids while the rows stay absent
        # (after the window, the first read seeds them and these read the rows instead)
        response = await owner_client.head(f"/models/{repo.id}/resolve/dev/README.md")
        assert response.headers["ETag"] == blob("hello")
        response = await owner_client.post(
            f"/api/models/{repo.id}/paths-info/dev", data={"paths": ["README.md", "w.bin"]}
        )
        by_path = {entry["path"]: entry["oid"] for entry in response.json()}
        assert by_path == {"README.md": blob("hello"), "w.bin": sha(b"w main")}


# --- D5: an identical re-commit with no row is a no-op, not a 500 ---------------------


async def test_an_integrity_error_on_main_is_not_the_contract_refusal(m, owner_client):
    """Only a side branch's refusal is the old key's: a database error on main is not
    reported as a 409 to wait for the contract (PostgreSQL only: a trigger raises it)."""
    if m.cfg.app.db_backend != "postgres":
        pytest.skip("the unique violation is a PL/pgSQL trigger")
    repo = await Repo(m, owner_client, "d2-main-error").create()
    await repo.commit(_file("keep.txt", "keep"))
    db = m.db.db
    db.execute_sql(
        "CREATE OR REPLACE FUNCTION d2_main_violation() RETURNS trigger AS $$ BEGIN "
        "IF NEW.branch = 'main' AND NEW.path_in_repo = 'boom.txt' THEN "
        "RAISE EXCEPTION 'forced unique violation' USING ERRCODE = '23505'; "
        "END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
    )
    db.execute_sql(
        "CREATE TRIGGER d2_main_violation BEFORE INSERT ON \"file\" FOR EACH ROW "
        "EXECUTE FUNCTION d2_main_violation()"
    )
    try:
        response = await _post_commit(owner_client, repo, "main", _header(), _text_file("boom.txt", "boom"))
        assert response.status_code == 500, response.text
        assert "contract" not in response.text
        assert "forced unique violation" in response.text
    finally:
        db.execute_sql("DROP TRIGGER IF EXISTS d2_main_violation ON \"file\"")
        db.execute_sql("DROP FUNCTION IF EXISTS d2_main_violation()")


async def test_d5_identical_recommit_with_a_missing_row_is_a_noop(m, owner_client):
    repo = await Repo(m, owner_client, "d5-noop").create()
    await repo.commit(_file("README.md", "hello"))
    await _branch(owner_client, repo, "dev")
    from kohakuhub.db import File

    File.delete().where((File.repository == _repo_row(repo)) & (File.branch == "dev")).execute()
    head_before = await _head(owner_client, repo, "dev")

    response = await _post_commit(owner_client, repo, "dev", _header(), _text_file("README.md", "hello"))
    assert response.status_code == 200, response.text
    assert response.json()["commitOid"] == head_before
    assert await _head(owner_client, repo, "dev") == head_before
    assert _rows(_repo_row(repo), "dev") == {}
    assert await _tree(owner_client, repo, "dev") == {"README.md": blob("hello")}
