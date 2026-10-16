"""reset, merge, revert and super-squash on a branch that is not there, and a merge with nothing to merge (#103).

`reset` of a branch LakeFS does not have was a 500 (its 404 reached a generic handler, which also marked the outcome of a
branch that never existed as unknown); `merge` with nothing to merge was a 500 (LakeFS says `no changes` with a 400);
`revert`, `super-squash` and the availability check answered 404 without `X-Error-Code`, so `huggingface_hub` could not tell a
revision that is not there from any other 404 (`super_squash_history` documents `RevisionNotFoundError`).
Everything runs against the real database, LakeFS and bucket; what real LakeFS cannot be made to do (fail one call) is injected.
"""

import httpx
import pytest

from test.kohakuhub.api.commit.test_availability import Repo, _file, _live
from test.kohakuhub.support.db import history_operations_need_postgres
from test.kohakuhub.support.fakes import lakefs_error

pytestmark = [history_operations_need_postgres, pytest.mark.usefixtures("commit_ids_in_full")]


@pytest.fixture
def m(prepared_backend_test_state, monkeypatch):
    ns = type("M", (), {})()
    ns.cfg = _live("kohakuhub.config").cfg
    for flag in ("repository_reset_enabled", "repository_revert_enabled", "repository_squash_enabled"):
        monkeypatch.setattr(ns.cfg.app, flag, True)
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.records = _live("kohakuhub.api.commit.records")
    ns.reset = _live("kohakuhub.api.commit.reset")
    ns.revert = _live("kohakuhub.api.commit.revert")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    ns.rest = _live("kohakuhub.lakefs_rest_client")
    ns.rest._singleton_client = None
    ns.client = ns.lakefs.get_lakefs_client()
    monkeypatch.setattr(ns.records, "RETRY_DELAY", 0)
    yield ns
    ns.db.LfsObjectTombstone.delete().execute()
    ns.rest._singleton_client = None


async def _repo(m, owner_client, name):
    repo = await Repo(m, owner_client, name).create()
    first = await repo.commit(_file("a.txt", "one"), summary="one")
    second = await repo.commit(_file("b.txt", "two"), summary="two")
    assert (await owner_client.post(f"/api/models/{repo.id}/tag", json={"tag": "v1", "revision": "main"})).status_code == 200
    assert (await owner_client.post(f"/api/models/{repo.id}/branch", json={"branch": "dev", "revision": "main"})).status_code == 200
    return repo, first, second


def _revision_not_found(response, *words):
    assert response.status_code == 404, response.text
    assert response.headers["x-error-code"] == "RevisionNotFound"
    message = response.headers["x-error-message"]
    assert all(word in message for word in words), message


@pytest.fixture
def reconciliations(m, monkeypatch):
    """What `records.outcome_unknown` asks for, recorded."""
    asked = []
    monkeypatch.setattr(m.records, "enqueue_lfs_reconciliation", lambda: asked.append(1))
    return asked


# ----- reset


@pytest.mark.parametrize("branch", ["no-such-branch", "v1"])
async def test_reset_of_a_branch_that_is_not_there_is_revision_not_found(m, owner_client, reconciliations, branch):
    repo, first, _ = await _repo(m, owner_client, f"rs-{branch}")

    response = await owner_client.post(f"/api/models/{repo.id}/branch/{branch}/reset", json={"ref": first, "force": True})

    _revision_not_found(response, branch)
    assert reconciliations == []  # nothing happened to that branch: its outcome is not "unknown"


async def test_reset_to_a_commit_that_is_not_there_is_revision_not_found(m, owner_client):
    repo, _, _ = await _repo(m, owner_client, "rs-ref")

    response = await owner_client.post(f"/api/models/{repo.id}/branch/dev/reset", json={"ref": "0" * 64})

    _revision_not_found(response, "Commit not found")
    assert response.json()["detail"]["error"] == f"Commit not found: {'0' * 64}"  # the words of the route are kept


async def test_reset_of_a_branch_that_is_there_is_unchanged(m, owner_client, reconciliations):
    repo, first, _ = await _repo(m, owner_client, "rs-ok")

    response = await owner_client.post(f"/api/models/{repo.id}/branch/dev/reset", json={"ref": first})

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


@pytest.mark.parametrize("failure,status,code", [("503", 500, "ServerError"), ("connect", 500, "ServerError")])
async def test_a_reset_that_fails_in_lakefs_is_an_error_and_its_outcome_unknown(m, owner_client, monkeypatch, reconciliations, failure, status, code):
    """Targeted mock: a failure of LakeFS in the middle cannot be made to happen by hand."""
    repo, first, _ = await _repo(m, owner_client, f"rs-fail-{failure}")

    async def failing(*args, **kwargs):
        raise lakefs_error(503, "unavailable") if failure == "503" else httpx.ConnectError("lakefs is down")

    monkeypatch.setattr(m.reset, "reset_branch", failing)
    response = await owner_client.post(f"/api/models/{repo.id}/branch/dev/reset", json={"ref": first})

    assert response.status_code == status and response.headers["x-error-code"] == code, response.text
    assert "Reset failed" in response.headers["x-error-message"]
    assert reconciliations == [1]  # whether it landed is not known


# ----- merge


async def test_a_merge_with_nothing_to_merge_is_a_200_that_says_so(m, owner_client):
    repo, first, _ = await _repo(m, owner_client, "mg-nothing")
    head = await repo.head()
    ancestors = {"a branch with nothing new": "dev", "a tag that is an ancestor": "v1", "a commit id that is an ancestor": first}

    for kind, source in ancestors.items():
        response = await owner_client.post(f"/api/models/{repo.id}/merge/{source}/into/main", json={})

        assert response.status_code == 200, (kind, response.text)
        body = response.json()
        assert body["success"] is True and "already up to date" in body["message"], kind
        assert await repo.head() == head, kind  # no commit was made


async def test_a_merge_with_something_to_merge_still_merges_and_the_next_one_is_up_to_date(m, owner_client):
    repo, _, _ = await _repo(m, owner_client, "mg-something")
    await repo.commit(_file("only-dev.txt", "dev"), branch="dev", summary="dev only")
    before = await repo.head()

    merged = await owner_client.post(f"/api/models/{repo.id}/merge/dev/into/main", json={})
    again = await owner_client.post(f"/api/models/{repo.id}/merge/dev/into/main", json={})

    assert merged.status_code == 200 and "Successfully merged" in merged.json()["message"], merged.text
    assert await repo.head() != before
    after = await repo.head()
    assert again.status_code == 200 and "already up to date" in again.json()["message"]
    assert await repo.head() == after


async def test_a_merge_with_a_branch_that_is_not_there_is_still_a_404(m, owner_client):
    repo, _, _ = await _repo(m, owner_client, "mg-missing")

    source = await owner_client.post(f"/api/models/{repo.id}/merge/no-such-branch/into/main", json={})
    destination = await owner_client.post(f"/api/models/{repo.id}/merge/main/into/no-such-branch", json={})

    _revision_not_found(source, "no-such-branch")
    _revision_not_found(destination, "no-such-branch")


async def test_a_merge_that_fails_in_lakefs_for_another_reason_is_still_a_500(m, owner_client, monkeypatch):
    repo, _, _ = await _repo(m, owner_client, "mg-fail")

    async def failing(self, **kwargs):
        raise lakefs_error(503, "unavailable")

    monkeypatch.setattr(type(m.client), "merge_into_branch", failing)
    response = await owner_client.post(f"/api/models/{repo.id}/merge/dev/into/main", json={})

    assert response.status_code == 500
    assert "Merge failed" in response.text


# ----- revert, super-squash and the availability check


async def test_revert_on_a_branch_or_a_commit_that_is_not_there_is_revision_not_found(m, owner_client):
    repo, first, second = await _repo(m, owner_client, "rv-missing")

    branch = await owner_client.post(f"/api/models/{repo.id}/branch/no-such-branch/revert", json={"ref": second})
    commit = await owner_client.post(f"/api/models/{repo.id}/branch/dev/revert", json={"ref": "0" * 64})

    _revision_not_found(branch, "Branch not found: no-such-branch")
    _revision_not_found(commit, "Commit not found")


async def test_super_squash_of_a_branch_that_is_not_there_is_revision_not_found(m, owner_client):
    repo, _, _ = await _repo(m, owner_client, "sq-missing")

    response = await owner_client.post(f"/api/models/{repo.id}/super-squash/no-such-branch", json={"message": "squash"})

    _revision_not_found(response, "Branch not found: no-such-branch")


async def test_the_availability_check_on_a_branch_that_is_not_there_is_revision_not_found(m, owner_client):
    repo, first, _ = await _repo(m, owner_client, "av-missing")

    response = await repo.preflight(first, branch="no-such-branch")

    _revision_not_found(response, "Branch not found: no-such-branch")


async def test_a_refusal_that_is_not_a_missing_revision_keeps_its_status_and_has_no_revision_code(m, owner_client):
    """A reset of `main` without `force` is refused for its own reason."""
    repo, first, _ = await _repo(m, owner_client, "rf-main")

    response = await owner_client.post(f"/api/models/{repo.id}/branch/main/reset", json={"ref": first})

    assert response.status_code == 400
    assert response.headers.get("x-error-code") != "RevisionNotFound"


# ----- the other ways a history operation fails (revert, a ref LakeFS cannot answer, a fault of ours)


@pytest.mark.parametrize("route", ["reset", "revert"])
async def test_a_ref_that_lakefs_cannot_answer_is_its_error_and_not_a_missing_commit(m, owner_client, monkeypatch, route):
    """It was `404 Commit not found` whatever the failure was; only "no such commit" is that."""
    repo, first, _ = await _repo(m, owner_client, f"{route}-ref503")

    async def failing(self, **kwargs):
        raise lakefs_error(503, "unavailable")

    monkeypatch.setattr(type(m.client), "get_commit", failing)
    payload = {"ref": first, "force": True} if route == "reset" else {"ref": first}
    response = await owner_client.post(f"/api/models/{repo.id}/branch/dev/{route}", json=payload)

    assert response.status_code == 500, response.text
    assert response.headers["x-error-code"] == "ServerError"
    assert "failed" in response.headers["x-error-message"]


@pytest.mark.parametrize("failure", ["503", "connect", "404"])
async def test_a_revert_that_fails_in_lakefs_is_its_error_and_its_outcome_unknown_only_when_it_is(m, owner_client, monkeypatch, reconciliations, failure):
    repo, _, second = await _repo(m, owner_client, f"rv-fail-{failure}")

    async def failing(*args, **kwargs):
        if failure == "connect":
            raise httpx.ConnectError("lakefs is down")
        raise lakefs_error(int(failure), "the branch is gone" if failure == "404" else "unavailable")

    monkeypatch.setattr(m.revert, "revert_commit", failing)
    response = await owner_client.post(f"/api/models/{repo.id}/branch/dev/revert", json={"ref": second})

    if failure == "404":
        _revision_not_found(response, "dev")
        assert reconciliations == []
    else:
        assert response.status_code == 500 and response.headers["x-error-code"] == "ServerError", response.text
        assert "Revert failed" in response.headers["x-error-message"]
        assert reconciliations == [1]


@pytest.mark.parametrize("route,module,function", [("reset", "reset", "reset_branch"), ("revert", "revert", "revert_commit")])
async def test_a_fault_of_ours_in_a_history_operation_is_a_500_and_its_outcome_unknown(m, owner_client, monkeypatch, reconciliations, route, module, function):
    repo, first, second = await _repo(m, owner_client, f"{route}-bug")

    async def broken(*args, **kwargs):
        raise ValueError("a bug of ours")

    monkeypatch.setattr(getattr(m, module), function, broken)
    payload = {"ref": first, "force": True} if route == "reset" else {"ref": second}
    response = await owner_client.post(f"/api/models/{repo.id}/branch/dev/{route}", json=payload)

    assert response.status_code == 500
    assert "a bug of ours" in response.text
    assert reconciliations == [1]
