"""Issue #11, stage 2: a history operation on a side branch writes that branch's
File rows and leaves main's alone (real database, LakeFS and bucket)."""

import hashlib

import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _file
from test.kohakuhub.api.test_branch_reset import m, _reset  # noqa: F401  (fixtures and helper)
from test.kohakuhub.support.db import history_operations_need_postgres

pytestmark = history_operations_need_postgres


def _sha1(text):
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _row(m, repo, branch, path):
    F = m.db.File
    return F.get_or_none(
        (F.repository == m.db.Repository.get(m.db.Repository.full_id == repo.id))
        & (F.branch == branch)
        & (F.path_in_repo == path)
    )


async def test_a_reset_of_a_side_branch_writes_its_own_rows(m, owner_client):
    repo = await Repo(m, owner_client, "side-reset").create()
    await repo.commit(_file("a.txt", "hello"))  # main: hello
    c1 = await repo.head()
    response = await owner_client.post(f"/api/models/{repo.id}/branch", json={"branch": "dev"})
    assert response.status_code == 200, response.text
    await repo.commit(_file("a.txt", "world"), _file("b.txt", "only on dev"), branch="dev")

    response = await _reset(repo, c1, branch="dev")
    assert response.status_code == 200, response.text

    assert _row(m, repo, "dev", "a.txt").sha256 == _sha1("hello")
    assert _row(m, repo, "dev", "b.txt").is_deleted is True
    assert _row(m, repo, "main", "a.txt").sha256 == _sha1("hello")
    assert _row(m, repo, "main", "b.txt") is None
