"""Whether a commit can be reverted, or a branch reset to it, known up front.

Everything runs against the real database, LakeFS and bucket. The property
test compares every prediction with what LakeFS really does.
"""

import base64
import hashlib
import importlib
import json
import random

import httpx
import pytest

from kohakuhub import lakefs_rest_client
from test.kohakuhub.api.helpers import encode_ndjson

REPO = "owner/avail-demo"


def _live(module):
    """The currently registered module (see test_storage_cleanup.py)."""
    return importlib.import_module(module)


@pytest.fixture
def m(prepared_backend_test_state, monkeypatch):
    lakefs_rest_client._singleton_client = None
    cfg = _live("kohakuhub.config").cfg
    monkeypatch.setattr(cfg.app, "repository_revert_enabled", True)
    monkeypatch.setattr(cfg.app, "repository_reset_enabled", True)
    ns = type("M", (), {})()
    ns.cfg = cfg
    ns.db = _live("kohakuhub.db")
    ns.gc = _live("kohakuhub.lfs_gc")
    ns.avail = _live("kohakuhub.api.commit.availability")
    ns.s3 = _live("kohakuhub.utils.s3").get_s3_client()
    ns.lakefs = _live("kohakuhub.utils.lakefs")
    yield ns
    ns.db.LfsObjectTombstone.delete().execute()
    lakefs_rest_client._singleton_client = None


def _lfs(path, content):
    oid = hashlib.sha256(content).hexdigest()
    return {
        "key": "lfsFile",
        "value": {"path": path, "oid": oid, "size": len(content), "algo": "sha256"},
    }


def _file(path, text):
    return {
        "key": "file",
        "value": {
            "path": path,
            "content": base64.b64encode(text.encode()).decode(),
            "encoding": "base64",
        },
    }


def _delete(path):
    return {"key": "deletedFile", "value": {"path": path}}


class Repo:
    """A repository built commit by commit through the API."""

    def __init__(self, m, client, name):
        self.m, self.client, self.name = m, client, name
        self.id = f"owner/{name}"

    async def create(self):
        response = await self.client.post(
            "/api/repos/create", json={"type": "model", "name": self.name}
        )
        assert response.status_code == 200, response.text
        return self

    def put(self, content):
        oid = hashlib.sha256(content).hexdigest()
        self.m.s3.put_object(
            Bucket=self.m.cfg.s3.bucket, Key=f"lfs/{oid[:2]}/{oid[2:4]}/{oid}", Body=content
        )
        return oid

    async def commit(self, *ops, branch="main"):
        for op in ops:
            if op["key"] == "lfsFile":
                self.put(op["value"].pop("_content"))
        response = await self.client.post(
            f"/api/models/{self.id}/commit/{branch}",
            content=encode_ndjson([{"key": "header", "value": {"summary": "change"}}, *ops]),
            headers={"Content-Type": "application/x-ndjson"},
        )
        assert response.status_code == 200, response.text
        return response.json()["commitOid"]

    async def head(self, branch="main"):
        return (await self.client.get(f"/api/models/{self.id}/revision/{branch}")).json()["sha"]

    async def preflight(self, commit, branch="main", client=None):
        return await (client or self.client).get(
            f"/api/models/{self.id}/commit/{commit}/operations", params={"branch": branch}
        )

    async def quick(self, commits, branch="main", client=None):
        return await (client or self.client).post(
            f"/api/models/{self.id}/commits/{branch}/operations", json={"commit_ids": commits}
        )

    @property
    def lakefs_repo(self):
        repo = self.m.db.Repository.get(self.m.db.Repository.full_id == self.id)
        return self.m.lakefs.resolve_lakefs_repo(repo)


def lfs(path, content):  # noqa: D103 - an LFS op carrying its content
    op = _lfs(path, content)
    op["value"]["_content"] = content
    return op


async def _linear(m, owner_client, name="avail-linear"):
    """c1 adds a.bin and r.txt; c2 changes a.bin; c3 adds b.bin; c4 changes
    r.txt; c5 changes a.bin again."""
    repo = await Repo(m, owner_client, name).create()
    initial = await repo.head()
    c1 = await repo.commit(lfs("a.bin", b"a v1"), _file("r.txt", "r1"))
    c2 = await repo.commit(lfs("a.bin", b"a v2"))
    c3 = await repo.commit(lfs("b.bin", b"b v1"))
    c4 = await repo.commit(_file("r.txt", "r2"))
    c5 = await repo.commit(lfs("a.bin", b"a v3"))
    return repo, [initial, c1, c2, c3, c4, c5]


def _verdict(body, op):
    return body[op]["available"], body[op].get("reason")


# ----- revert -----


async def test_revert_rules_on_a_linear_history(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client)

    body = (await repo.preflight(initial)).json()
    assert _verdict(body, "revert") == (False, "initial_commit")
    body = (await repo.preflight(c2)).json()
    assert _verdict(body, "revert") == (False, "conflict")  # c5 changed a.bin since
    assert body["revert"]["conflicts"] == ["a.bin"]
    for commit, files in ((c3, 1), (c4, 1), (c5, 1), (c1, 2)):
        body = (await repo.preflight(commit)).json()
        if commit == c1:  # a.bin changed since: a conflict too
            assert _verdict(body, "revert") == (False, "conflict")
            continue
        assert _verdict(body, "revert") == (True, None), (commit, body)
        assert body["revert"]["files"] == files

    # Undone by hand: reverting it again changes nothing
    await repo.commit(_file("r.txt", "r1"))
    body = (await repo.preflight(c4)).json()
    assert _verdict(body, "revert") == (False, "no_changes")


async def test_reverting_needs_the_versions_it_restores(m, owner_client):
    repo = await Repo(m, owner_client, "avail-lfs").create()
    c1 = await repo.commit(lfs("w.bin", b"w v1"), lfs("x.bin", b"x v1"))
    c2 = await repo.commit(lfs("w.bin", b"w v2"))
    old = hashlib.sha256(b"w v1").hexdigest()
    assert _verdict((await repo.preflight(c2)).json(), "revert") == (True, None)

    # Collected by garbage collection: a tombstone says so
    m.db.LfsObjectTombstone.create(sha256=old, state=m.gc.DELETED)
    body = (await repo.preflight(c2)).json()
    assert _verdict(body, "revert") == (False, "lfs_missing")
    assert body["revert"]["missing_lfs"] == ["w.bin"]
    body = (await repo.preflight(c1)).json()
    assert _verdict(body, "reset") == (False, "lfs_missing")

    # Missing from storage for another reason: the full check sees it too
    m.db.LfsObjectTombstone.delete().execute()
    m.s3.delete_object(Bucket=m.cfg.s3.bucket, Key=m.gc.lfs_key(old))
    assert _verdict((await repo.preflight(c2)).json(), "revert") == (False, "lfs_missing")


# ----- reset -----


async def test_reset_rules(m, owner_client):
    repo, (initial, c1, c2, c3, c4, c5) = await _linear(m, owner_client, "avail-reset")

    body = (await repo.preflight(c5)).json()
    assert _verdict(body, "reset") == (False, "already_current")
    body = (await repo.preflight(c3)).json()
    assert _verdict(body, "reset") == (True, None)
    assert body["reset"]["files"] == 2  # a.bin and r.txt differ from the head
    assert body["reset"]["requires_force"] is True  # main

    # Same content as the head, another commit
    t = await repo.commit(_file("t.txt", "temporary"))
    await repo.commit(_delete("t.txt"))
    body = (await repo.preflight(t)).json()
    assert _verdict(body, "reset") == (True, None)
    body = (await repo.preflight(c5)).json()  # the head's content again
    assert _verdict(body, "reset") == (False, "no_changes")

    # Another branch needs no force
    response = await owner_client.post(f"/api/models/{repo.id}/branch", json={"branch": "dev"})
    assert response.status_code == 200
    body = (await repo.preflight(c3, branch="dev")).json()
    assert body["reset"]["requires_force"] is False


# ----- who may ask, and when nothing is evaluated -----


async def test_permissions_capabilities_and_missing_refs(m, owner_client, visitor_client, app):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "avail-perm")

    body = (await repo.preflight(c1, client=visitor_client)).json()
    assert body["can_write"] is False
    assert _verdict(body, "revert") == (False, "forbidden")
    assert _verdict(body, "reset") == (False, "forbidden")
    body = (await repo.quick([c1], client=visitor_client)).json()
    assert body["can_write"] is False and body["commits"] == {}

    m.cfg.app.repository_revert_enabled = False
    body = (await repo.preflight(c1)).json()
    assert _verdict(body, "revert") == (False, "disabled")
    assert body["operations"] == {"revert": False, "reset": True}

    assert (await repo.preflight("f" * 64)).status_code == 404
    assert (await repo.preflight(c1, branch="missing")).status_code == 404
    assert (await repo.quick([c1], branch="missing")).status_code == 404
    response = await owner_client.get(
        "/api/models/owner/avail-nope/commit/abc/operations", params={"branch": "main"}
    )
    assert response.status_code == 404
    response = await owner_client.post(
        "/api/models/owner/avail-nope/commits/main/operations", json={"commit_ids": []}
    )
    assert response.status_code == 404
    response = await repo.quick(["x"] * 101)
    assert response.status_code == 422  # at most one page

    # A private repository stays invisible to outsiders
    private = await Repo(m, owner_client, "avail-private").create()
    response = await owner_client.put(
        f"/api/models/{private.id}/settings", json={"visibility": "private"}
    )
    assert response.status_code == 200
    head = await private.head()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        assert (await private.preflight(head, client=anonymous)).status_code == 404
        assert (await private.quick([head], client=anonymous)).status_code == 404


# ----- the commit list page -----


async def test_the_list_marks_only_what_is_proven(m, owner_client, monkeypatch):
    repo, commits = await _linear(m, owner_client, "avail-quick")
    initial, c1, c2, c3, c4, c5 = commits

    body = (await repo.quick(commits + ["f" * 64])).json()
    assert body["head"] == c5
    assert body["commits"][initial]["revert"] == {
        "available": False,
        "reason": "initial_commit",
        "message": body["commits"][initial]["revert"]["message"],
    }
    assert body["commits"][c5]["reset"]["reason"] == "already_current"
    # Conflicts are not decided on the list: unknown, never "available"
    assert body["commits"][c2]["revert"] == {"available": None}
    assert "f" * 64 not in body["commits"]  # not a commit of this repository

    # With collected objects the list checks them, exactly
    old = hashlib.sha256(b"a v1").hexdigest()
    m.db.LfsObjectTombstone.create(sha256=old, state=m.gc.DELETED)
    body = (await repo.quick(commits)).json()
    assert body["commits"][c1]["reset"]["reason"] == "lfs_missing"
    assert body["commits"][c2]["revert"]["reason"] == "lfs_missing"
    assert body["commits"][c3]["reset"] == {"available": None}  # a.bin is v2 there

    # Past the budget the rest stays unknown
    monkeypatch.setattr(m.avail, "QUICK_BUDGET", 1)
    body = (await repo.quick(commits)).json()
    assert body["commits"][c1]["reset"] == {"available": None}


# ----- the predictions match what LakeFS does -----


async def _lakefs_revert_outcome(m, repo, commit, head):
    """Revert ``commit`` on a throwaway branch at ``head``; what happened."""
    client = m.lakefs.get_lakefs_client()
    branch = f"probe-{commit[:10]}"
    await client.create_branch(repository=repo.lakefs_repo, name=branch, source=head)
    try:
        await client.revert_branch(
            repository=repo.lakefs_repo, branch=branch, ref=commit, parent_number=1
        )
        return "applied"
    except httpx.HTTPStatusError as e:
        return {409: "conflict", 400: "rejected"}.get(
            e.response.status_code, e.response.status_code
        )
    finally:
        await client.delete_branch(repository=repo.lakefs_repo, branch=branch)


async def test_revert_predictions_match_lakefs_on_a_tangled_history(m, owner_client):
    rng = random.Random(115)
    repo = await Repo(m, owner_client, "avail-tangled").create()
    live: dict[str, bytes | str] = {}
    version = 0
    for step in range(36):
        ops = []
        for _ in range(rng.randint(1, 4)):
            version += 1
            path = rng.choice(["m/a.bin", "m/b.bin", "m/c.txt", "n/d.bin", "n/e.txt", "f.bin"])
            if path in live and rng.random() < 0.3:
                ops = [op for op in ops if op["value"]["path"] != path] + [_delete(path)]
                live.pop(path)
            elif path.endswith(".bin"):
                ops = [op for op in ops if op["value"]["path"] != path]
                ops.append(lfs(path, f"{path} {version}".encode()))
                live[path] = b""
            else:
                ops = [op for op in ops if op["value"]["path"] != path] + [
                    _file(path, f"{version}")
                ]
                live[path] = ""
        if step == 20:  # a side branch, merged back
            await owner_client.post(f"/api/models/{repo.id}/branch", json={"branch": "side"})
            await repo.commit(lfs("side.bin", b"side 1"), branch="side")
            await repo.commit(_file("m/c.txt", "side"), branch="side")
            response = await owner_client.post(
                f"/api/models/{repo.id}/merge/side/into/main", json={"strategy": "source-wins"}
            )
            assert response.status_code == 200, response.text
        if ops:
            await repo.commit(*ops)

    head = await repo.head()
    client = m.lakefs.get_lakefs_client()
    log = (await client.log_commits(repository=repo.lakefs_repo, ref="main", amount=1000))[
        "results"
    ]
    outcomes = {}
    for entry in log:
        body = (await repo.preflight(entry["id"])).json()
        predicted = body["revert"]
        actual = await _lakefs_revert_outcome(m, repo, entry["id"], head)
        expected = {
            None: "applied",
            "conflict": "conflict",
            "no_changes": "rejected",
            "initial_commit": "rejected",
        }[predicted.get("reason")]
        assert expected == actual, (entry["id"], entry.get("message"), predicted, actual)
        outcomes[predicted.get("reason")] = outcomes.get(predicted.get("reason"), 0) + 1
    # The history exercised every outcome
    assert {None, "conflict", "initial_commit"} <= set(outcomes), outcomes
    assert len(log) > 36


async def test_big_diffs_page_and_list_instead_of_one_stat_each(m, owner_client, monkeypatch):
    repo, commits = await _linear(m, owner_client, "avail-paging")
    expected = [(await repo.preflight(commit)).json() for commit in commits]
    # One entry per page, and a whole-ref listing instead of stats
    monkeypatch.setattr(m.avail, "PAGE", 1)
    monkeypatch.setattr(m.avail, "LISTING_THRESHOLD", 1)
    assert [(await repo.preflight(commit)).json() for commit in commits] == expected


async def test_lakefs_failures_other_than_not_found_surface(m, owner_client, monkeypatch):
    repo, (initial, c1, *_rest) = await _linear(m, owner_client, "avail-errors")
    routes = _live("kohakuhub.api.commit.routers.availability")

    def failing(name):
        async def fail(**kwargs):
            request = httpx.Request("GET", "http://lakefs/x")
            raise httpx.HTTPStatusError(
                "unavailable", request=request, response=httpx.Response(503, request=request)
            )

        return fail

    real = m.lakefs.get_lakefs_client()
    for method in ("get_branch", "get_commit", "stat_object"):
        broken = type("Broken", (), {})()
        for attr in ("get_branch", "get_commit", "stat_object", "diff_refs", "list_objects"):
            setattr(broken, attr, getattr(real, attr))
        setattr(broken, method, failing(method))
        monkeypatch.setattr(routes, "get_lakefs_client", lambda broken=broken: broken)
        with pytest.raises(httpx.HTTPStatusError):
            await routes.commit_operations(
                "model", "owner", "avail-errors", c1, "main", user=m.db.User.get(username="owner")
            )


async def test_the_list_reports_an_operation_disabled_on_its_own(m, owner_client):
    repo, commits = await _linear(m, owner_client, "avail-one-off")
    m.cfg.app.repository_reset_enabled = False
    body = (await repo.quick(commits)).json()
    assert body["operations"] == {"revert": True, "reset": False}
    assert {c["reset"]["reason"] for c in body["commits"].values()} == {"disabled"}
    assert body["commits"][commits[0]]["revert"]["reason"] == "initial_commit"
    m.cfg.app.repository_revert_enabled = False
    assert (await repo.quick(commits)).json()["commits"] == {}  # nothing to act on
