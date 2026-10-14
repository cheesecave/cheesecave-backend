"""Branch and tag names with a `/` (`feature/x`, `release/v1.0`) on every route that takes a revision (#67).

The Hub takes what git takes; LakeFS takes letters, digits, `_` and `-` only, so the names are spelled for LakeFS in
one place (`kohakuhub.utils.ref_name`). Real LakeFS and database. The clients send the name percent-encoded
(`feature%2Fx`); a name sent with its slashes as they are takes the same routes.
"""

import base64
import json

import pytest

from test.kohakuhub.support.db import history_operations_need_postgres

B = "/api/models/owner/demo-model"
ND = {"Content-Type": "application/x-ndjson"}
FORMS = [("encoded", "feature%2Fx"), ("plain", "feature/x")]


@pytest.fixture(autouse=True)
async def only_main(owner_client):
    """Every test starts and ends with `main` alone: LakeFS keeps what a failed test left."""

    async def clear():
        refs = (await owner_client.get(f"{B}/refs")).json()
        for branch in refs["branches"]:
            if branch["name"] != "main":
                await owner_client.delete(f"{B}/branch/{branch['name']}")
        for tag in refs["tags"]:
            await owner_client.delete(f"{B}/tag/{tag['name']}")

    await clear()
    yield
    await clear()


async def _commit(client, revision_path, path, text, parent=None):
    header = {"summary": f"add {path}"}
    if parent:
        header["parentCommit"] = parent
    content = base64.b64encode(text.encode()).decode()
    body = '{"key":"header","value":%s}\n{"key":"file","value":{"path":"%s","content":"%s","encoding":"base64"}}\n' % (
        json.dumps(header),
        path,
        content,
    )
    return await client.post(f"{B}/commit/{revision_path}", content=body, headers=ND)


async def _branches(client):
    return {b["name"]: b["targetCommit"] for b in (await client.get(f"{B}/refs")).json()["branches"]}


async def _tags(client):
    return {t["name"]: t["targetCommit"] for t in (await client.get(f"{B}/refs")).json()["tags"]}


@pytest.mark.parametrize("form, path", FORMS)
async def test_a_branch_with_a_slash_is_made_listed_and_deleted(owner_client, form, path):
    made = await owner_client.post(f"{B}/branch/{path}", json={})
    assert made.status_code == 200, made.text
    assert "feature/x" in await _branches(owner_client)
    assert (await owner_client.post(f"{B}/branch/{path}", json={})).status_code == 409  # a second time
    deleted = await owner_client.delete(f"{B}/branch/{path}")
    assert deleted.status_code == 200, deleted.text
    assert "feature/x" not in await _branches(owner_client)


async def test_the_json_route_takes_a_slash_too(owner_client):
    made = await owner_client.post(f"{B}/branch", json={"branch": "release/v1.0", "revision": "main"})
    assert made.status_code == 200, made.text
    assert "release/v1.0" in await _branches(owner_client)
    assert (await owner_client.delete(f"{B}/branch/release%2Fv1.0")).status_code == 200


@pytest.mark.parametrize("form, path", FORMS)
async def test_every_read_and_write_of_a_slash_branch(owner_client, form, path):
    c = owner_client
    assert (await c.post(f"{B}/branch/{path}", json={})).status_code == 200
    main_sha = (await c.get(f"{B}/revision/main")).json()["sha"]

    landed = await _commit(c, path, "dir/a.txt", "hello", parent=main_sha)
    assert landed.status_code == 200, landed.text
    sha = landed.json()["commitOid"]

    info = await c.get(f"{B}/revision/{path}")
    assert info.status_code == 200 and info.json()["sha"] == sha
    assert info.json()["sha"] != (await c.get(f"{B}/revision/main")).json()["sha"]  # main did not move
    assert (await _branches(c))["feature/x"] == sha

    tree = await c.get(f"{B}/tree/{path}?recursive=true")
    assert tree.status_code == 200, tree.text
    assert "dir/a.txt" in [item["path"] for item in tree.json()]
    subtree = await c.get(f"{B}/tree/{path}/dir")
    assert [item["path"] for item in subtree.json()] == ["dir/a.txt"]
    paths = await c.post(f"{B}/paths-info/{path}", data={"paths": ["dir/a.txt"]})
    assert [item["path"] for item in paths.json()] == ["dir/a.txt"]

    for method in ("get", "head"):
        got = await getattr(c, method)(f"/models/owner/demo-model/resolve/{path}/dir/a.txt", follow_redirects=False)
        assert got.status_code in (200, 302, 307), (method, got.status_code, got.text[:100])
        assert got.headers["x-repo-commit"] == sha
    assert (await c.get(f"/models/owner/demo-model/resolve/{path}/missing.txt")).headers["x-error-code"] == "EntryNotFound"

    log = await c.get(f"{B}/commits/{path}")
    assert log.status_code == 200 and log.json()[0]["id"] == sha
    ops = await c.post(f"{B}/commits/{path}/operations", json={"commit_ids": [sha]})
    assert ops.status_code == 200
    assert ops.json()["branch"] == "feature/x" and ops.json()["head"] == sha  # the Hub's name comes back

    assert (await c.delete(f"{B}/branch/{path}")).status_code == 200


async def test_the_full_ref_of_a_slash_branch_names_the_branch(owner_client):
    """`refs/heads/feature/x`, percent-encoded as `huggingface_hub` quotes a revision, is the branch `feature/x`."""
    c = owner_client
    assert (await c.post(f"{B}/branch/feature%2Fx", json={})).status_code == 200
    sha = (await _commit(c, "feature%2Fx", "dir/a.txt", "hello")).json()["commitOid"]
    full = "refs%2Fheads%2Ffeature%2Fx"
    assert (await c.get(f"{B}/revision/{full}")).json()["sha"] == sha
    assert "dir/a.txt" in [i["path"] for i in (await c.get(f"{B}/tree/{full}?recursive=true")).json()]
    head = await c.head(f"/models/owner/demo-model/resolve/{full}/dir/a.txt")
    assert head.status_code == 200 and head.headers["x-repo-commit"] == sha
    miss = await c.get(f"{B}/revision/refs%2Fheads%2Ffeature%2Fnope")
    assert miss.status_code == 404 and miss.headers["x-error-code"] == "RevisionNotFound"


async def test_a_file_in_main_is_not_taken_for_a_branch(owner_client):
    """`main/dir/a.txt` stays the file `dir/a.txt` of main when no branch `main/dir` exists."""
    c = owner_client
    assert (await _commit(c, "main", "dir/a.txt", "x")).status_code == 200
    got = await c.get("/models/owner/demo-model/resolve/main/dir/a.txt", follow_redirects=False)
    assert got.status_code in (200, 302, 307)
    miss = await c.get("/models/owner/demo-model/resolve/nope/dir/a.txt")
    assert miss.status_code == 404 and miss.headers["x-error-code"] == "RevisionNotFound"
    miss = await c.get("/models/owner/demo-model/resolve/nope/x/y/z/w.txt")
    assert miss.status_code == 404 and miss.headers["x-error-code"] == "RevisionNotFound"


async def test_the_longest_branch_wins_over_a_file_path(owner_client):
    c = owner_client
    assert (await c.post(f"{B}/branch/a%2Fb%2Fc", json={})).status_code == 200
    assert (await _commit(c, "a%2Fb%2Fc", "f/g.txt", "deep")).status_code == 200
    got = await c.get("/models/owner/demo-model/resolve/a%2Fb%2Fc/f/g.txt", follow_redirects=False)
    assert got.status_code in (200, 302, 307)
    tree = await c.get(f"{B}/tree/a%2Fb%2Fc/f")
    assert [item["path"] for item in tree.json()] == ["f/g.txt"]


@pytest.mark.parametrize("path", ["release%2Fv1.0", "release/v1.0"])
async def test_a_tag_with_a_slash(owner_client, path):
    c = owner_client
    sha = (await c.get(f"{B}/revision/main")).json()["sha"]
    made = await c.post(f"{B}/tag/main", json={"tag": "release/v1.0"})
    assert made.status_code == 200, made.text
    assert (await _tags(c))["release/v1.0"] == sha
    again = await c.post(f"{B}/tag/main", json={"tag": "release/v1.0"})
    assert again.status_code == 409 and "already exists" in again.headers["x-error-message"]
    got = await c.get(f"{B}/revision/{path}")
    assert got.status_code == 200 and got.json()["sha"] == sha
    assert (await c.delete(f"{B}/tag/{path}")).status_code == 200
    assert "release/v1.0" not in await _tags(c)


async def test_a_branch_can_start_from_a_slash_branch_and_from_a_slash_tag(owner_client):
    c = owner_client
    assert (await c.post(f"{B}/branch/feature%2Fx", json={})).status_code == 200
    assert (await _commit(c, "feature%2Fx", "x.txt", "x")).status_code == 200
    assert (await c.post(f"{B}/branch/feature%2Fy", json={"startingPoint": "feature/x"})).status_code == 200
    branches = await _branches(c)
    assert branches["feature/y"] == branches["feature/x"]
    assert (await c.post(f"{B}/tag/feature%2Fx", json={"tag": "t/one"})).status_code == 200
    assert (await _tags(c))["t/one"] == branches["feature/x"]
    assert (await c.post(f"{B}/branch/from%2Ftag", json={"startingPoint": "t/one"})).status_code in (200, 404)


@history_operations_need_postgres  # revert and reset are PostgreSQL only
async def test_reset_revert_and_squash_of_a_slash_branch(owner_client):
    c = owner_client
    assert (await c.post(f"{B}/branch/feature%2Fx", json={})).status_code == 200
    base = (await c.get(f"{B}/revision/main")).json()["sha"]
    await _commit(c, "feature%2Fx", "one.txt", "1")
    second = (await _commit(c, "feature%2Fx", "two.txt", "2")).json()["commitOid"]
    reverted = await c.post(f"{B}/branch/feature%2Fx/revert", json={"ref": second})
    assert reverted.status_code == 200, reverted.text
    tree = [i["path"] for i in (await c.get(f"{B}/tree/feature%2Fx?recursive=true")).json()]
    assert "two.txt" not in tree and "one.txt" in tree
    reset = await c.post(f"{B}/branch/feature%2Fx/reset", json={"ref": base, "force": True})
    assert reset.status_code == 200, reset.text
    squashed = await c.post(f"{B}/super-squash/feature%2Fx", json={})
    assert squashed.status_code == 200, squashed.text
    assert "feature/x" in await _branches(c)


@history_operations_need_postgres  # merge is PostgreSQL only
async def test_merge_between_slash_branches(owner_client):
    c = owner_client
    for name in ("feature%2Fa", "feature%2Fb"):
        assert (await c.post(f"{B}/branch/{name}", json={})).status_code == 200
    assert (await _commit(c, "feature%2Fa", "from-a.txt", "a")).status_code == 200
    merged = await c.post(f"{B}/merge/feature%2Fa/into/feature%2Fb", json={})
    assert merged.status_code == 200, merged.text
    tree = [i["path"] for i in (await c.get(f"{B}/tree/feature%2Fb?recursive=true")).json()]
    assert "from-a.txt" in tree
    into_main = await c.post(f"{B}/merge/feature/b/into/main", json={})  # the slashes as they are
    assert into_main.status_code == 200, into_main.text
    assert "from-a.txt" in [i["path"] for i in (await c.get(f"{B}/tree/main?recursive=true")).json()]


@pytest.mark.parametrize("name", ["a..b", "a b", "x.lock", "a/.hidden", "a//b", "/lead", "trail/", "a~b", "a:b", "a?b", "-lead"])
async def test_a_name_git_refuses_is_a_bad_request(owner_client, name):
    made = await owner_client.post(f"{B}/branch", json={"branch": name})
    assert made.status_code == 400, (name, made.status_code, made.text)
    assert made.headers["x-error-code"] == "BadRequest"
    tag = await owner_client.post(f"{B}/tag", json={"tag": name})
    assert tag.status_code == 400, (name, tag.status_code)


async def test_a_branch_may_not_be_inside_another_or_hold_one(owner_client):
    """As in git, `feat` and `feat/x` cannot both exist: the first would hide the second from `resolve`."""
    c = owner_client
    assert (await c.post(f"{B}/branch/feat", json={})).status_code == 200
    inside = await c.post(f"{B}/branch/feat%2Fx", json={})
    assert inside.status_code == 409 and "'feat'" in inside.headers["x-error-message"]
    assert (await c.post(f"{B}/branch/d%2Fe%2Ff", json={})).status_code == 200
    for outer in ("d", "d/e"):
        holds = await c.post(f"{B}/branch", json={"branch": outer})
        assert holds.status_code == 409, (outer, holds.status_code)
    assert (await c.post(f"{B}/branch/d%2Fe%2Fg", json={})).status_code == 200  # a sibling is fine
    tag_in_branch_namespace = await c.post(f"{B}/tag/main", json={"tag": "feat/y"})
    assert tag_in_branch_namespace.status_code == 200  # branches and tags are separate namespaces
    again = await c.post(f"{B}/tag/main", json={"tag": "feat/y/z"})
    assert again.status_code == 409


async def test_the_names_that_were_always_fine_are_unchanged(owner_client):
    c = owner_client
    for name in ("dev", "my_branch", "my-branch", "v1", "A_b-C"):
        assert (await c.post(f"{B}/branch/{name}", json={})).status_code == 200
    assert {"dev", "my_branch", "my-branch", "v1", "A_b-C", "main"} <= set(await _branches(c))
    assert (await c.post(f"{B}/branch", json={"branch": "kh-reset-abc"})).status_code == 400  # still reserved
    assert (await c.delete(f"{B}/branch/main")).status_code == 400
    assert (await c.delete(f"{B}/branch/nope")).status_code in (404, 500)


async def test_the_files_of_a_slash_branch_are_its_own(owner_client):
    """The rows kept per branch (`File.branch`) follow the Hub's name: a sibling branch does not see them."""
    c = owner_client
    for name in ("feature%2Fone", "feature%2Ftwo"):
        assert (await c.post(f"{B}/branch/{name}", json={})).status_code == 200
    assert (await _commit(c, "feature%2Fone", "only-one.txt", "1")).status_code == 200
    one = [i["path"] for i in (await c.get(f"{B}/tree/feature%2Fone?recursive=true")).json()]
    two = [i["path"] for i in (await c.get(f"{B}/tree/feature%2Ftwo?recursive=true")).json()]
    assert "only-one.txt" in one and "only-one.txt" not in two
