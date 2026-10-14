"""Every place that says a commit says it in one form: the site's ``commit_id_format`` (#12).

``hub`` says the Hub's 40 hex, ``full`` the 64 LakeFS has. A response that said one commit in two lengths made
``model_info().sha`` (40) differ from ``model_info(revision="main").sha`` (64) and the ``X-Repo-Commit`` of a
download (64), so ``huggingface_hub`` kept its files in a folder named by one and looked in the other (diffusers'
``from_pretrained``, ``snapshot_download(revision=model_info().sha)``).

The census asks each route that says a commit and looks at every string, key and header for the ids of the
repository's commits: whatever it finds must be in the form of the site. A route added later that forgets
``hub_oid`` is found here.
"""

import base64
import importlib

import pytest

from test.kohakuhub.support.db import history_operations_need_postgres

B = "/api/models/owner/demo-model"
ND = {"Content-Type": "application/x-ndjson"}
# Text a person or LakeFS wrote about a commit (a message, the description of a revert) is not a place that says one
FREE_TEXT = {"message", "title", "description", "summary"}


def _scan(node, where, known, found, key=None):
    """Every occurrence of a known commit id in ``node``: (where, form)."""
    if isinstance(node, dict):
        for k, v in node.items():
            _scan(k, f"{where}{{key}}", known, found)  # an id used as a key
            _scan(v, f"{where}.{k}", known, found, key=k)
    elif isinstance(node, list):
        for v in node:
            _scan(v, f"{where}[]", known, found, key=key)
    elif isinstance(node, str) and key not in FREE_TEXT:
        for commit_id in known:
            if commit_id in node:
                found.add((where, "full"))
            elif commit_id[:40] in node:
                found.add((where, "hub"))


def _commit_lines(path, text):
    content = base64.b64encode(text.encode()).decode()
    return (
        '{"key":"header","value":{"summary":"s"}}\n'
        '{"key":"file","value":{"path":"%s","content":"%s","encoding":"base64"}}\n' % (path, content)
    )


async def _known_ids(owner_client):
    """The full ids of every commit of the repository, from LakeFS itself."""
    lakefs = importlib.import_module("kohakuhub.utils.lakefs")
    db = importlib.import_module("kohakuhub.db")
    repo = db.Repository.get(db.Repository.full_id == "owner/demo-model")
    client = lakefs.get_lakefs_client()
    ids = set()
    for branch in (await client.list_branches(repository=lakefs.resolve_lakefs_repo(repo)))["results"]:
        log = await client.log_commits(repository=lakefs.resolve_lakefs_repo(repo), ref=branch["id"], amount=200)
        ids.update(c["id"] for c in log["results"])
    return ids


@pytest.fixture
def set_format(monkeypatch):
    cfg = importlib.import_module("kohakuhub.config").cfg

    def set_to(value):
        monkeypatch.setattr(cfg.app, "commit_id_format", value)

    return set_to


@pytest.mark.parametrize("form", ["hub", "full"])
@history_operations_need_postgres  # revert, reset and squash are PostgreSQL only
async def test_every_route_that_says_a_commit_says_it_in_the_form_of_the_site(owner_client, set_format, form):
    c = owner_client
    set_format(form)
    side = f"side-{form}"
    # a richer history: commits on main, a side branch and a revert, a tag
    first = (await c.post(f"{B}/commit/main", content=_commit_lines("one.txt", "1"), headers=ND)).json()
    assert (await c.post(f"{B}/branch/{side}", json={"startingPoint": "main"})).status_code == 200
    second = (await c.post(f"{B}/commit/{side}", content=_commit_lines("two.txt", "2"), headers=ND)).json()
    revert = await c.post(f"{B}/branch/{side}/revert", json={"ref": second["commitOid"]})
    assert revert.status_code == 200, revert.text
    reset_to = (await c.post(f"{B}/commit/{side}", content=_commit_lines("three.txt", "3"), headers=ND)).json()["commitOid"]
    await c.post(f"{B}/commit/{side}", content=_commit_lines("four.txt", "4"), headers=ND)
    reset = await c.post(f"{B}/branch/{side}/reset", json={"ref": reset_to, "force": True})
    assert reset.status_code == 200, reset.text
    assert (await c.post(f"{B}/tag", json={"tag": f"v-{form}", "revision": "main"})).status_code == 200
    posted = await c.post(f"{B}/commit/main", content=_commit_lines("five.txt", "5"), headers=ND)  # the head the reads see

    known = await _known_ids(c)
    assert len(known) >= 6 and all(len(k) == 64 for k in known)
    head = sorted(known)[0]
    asked = [first["commitOid"], reset_to]  # the ids a client holds: what the site said
    answers = {}

    def record(name, response):
        assert response.status_code in (200, 302), (name, response.status_code, response.text[:200])
        found = set()
        try:
            _scan(response.json(), "$", known, found)
        except ValueError:
            pass
        for header, value in response.headers.items():
            _scan(value, f"header {header}", known, found)
        answers[name] = found

    main = (await c.get(f"{B}/revision/main")).json()["sha"]
    in_main = lambda commit: next(k for k in known if k.startswith(commit[:40]))  # noqa: E731

    record("POST commit", posted)
    record("info", await c.get(B))
    record("list", await c.get("/api/models?author=owner"))
    record("list expand=sha", await c.get("/api/models?author=owner&expand[]=sha"))
    discovery = importlib.import_module("kohakuhub.repository_discovery")
    db = importlib.import_module("kohakuhub.db")
    await discovery.index_repository(db.Repository.get(db.Repository.full_id == "owner/demo-model").id)
    record("discover", await c.get("/api/models/discover"))
    record("revision", await c.get(f"{B}/revision/main"))
    record("revision expand", await c.get(f"{B}/revision/{side}?expand=sha"))
    record("tree expand", await c.get(f"{B}/tree/main?expand=true&recursive=true"))
    record("paths-info expand", await c.post(f"{B}/paths-info/main", data={"paths": ["README.md", "one.txt"], "expand": "true"}))
    record("commits", await c.get(f"{B}/commits/main"))
    record("commits side", await c.get(f"{B}/commits/{side}"))
    record("commit detail", await c.get(f"{B}/commit/{main}"))
    record("commit detail (revert)", await c.get(f"{B}/commit/{revert.json()['new_commit_id']}"))
    record("commit diff", await c.get(f"{B}/commit/{main}/diff"))
    record("commit operations", await c.get(f"{B}/commit/{main}/operations"))
    record("commit unavailable-files", await c.get(f"{B}/commit/{main}/unavailable-files"))
    record("commits operations", await c.post(f"{B}/commits/{side}/operations", json={"commit_ids": asked + [side]}))
    record("commits unavailable-files", await c.post(f"{B}/commits/unavailable-files", json={"commit_ids": asked}))
    record("refs", await c.get(f"{B}/refs"))
    record("resolve HEAD", await c.head("/models/owner/demo-model/resolve/main/README.md"))
    record("resolve GET", await c.get("/models/owner/demo-model/resolve/main/README.md"))
    record("revert", revert)
    record("reset", reset)
    del head, in_main

    wrong = {"full" if form == "hub" else "hub"}
    bad = {name: sorted(f for f in found if f[1] in wrong) for name, found in answers.items()}
    bad = {name: places for name, places in bad.items() if places}
    assert not bad, f"say a commit in another form than {form!r}: {bad}"
    said = {name for name, found in answers.items() if found}
    # the census saw the routes say commits: it is not passing because it found nothing
    assert {"info", "revision", "commits", "commit detail", "refs", "resolve HEAD", "POST commit", "revert", "reset", "discover"} <= said


@pytest.mark.parametrize("form", ["hub", "full"])
async def test_the_workspace_feed_says_its_commits_in_the_form_of_the_site(owner_client, set_format, form):
    """The commit the home page links to: the web client opens ``commit/{sha}``."""
    set_format(form)
    made = (await owner_client.post(f"{B}/commit/main", content=_commit_lines(f"feed-{form}.txt", "f"), headers=ND)).json()
    full = next(k for k in await _known_ids(owner_client) if k.startswith(made["commitOid"][:40]))
    feed = await owner_client.get("/api/workspace/feed")
    assert feed.status_code == 200, feed.text
    shas = [i["commit"]["sha"] for i in feed.json()["items"] if i["kind"] == "commit" and i["commit"]]
    assert shas, "the feed has the commit"
    assert (full if form == "full" else full[:40]) in shas
    assert not [s for s in shas if s and len(s) == (40 if form == "full" else 64)], shas


@pytest.mark.parametrize("form", ["hub", "full"])
@history_operations_need_postgres  # revert, reset and squash are PostgreSQL only
async def test_a_squash_says_its_commit_in_the_form_of_the_site(owner_client, set_format, form):
    set_format(form)
    name = f"squash-forms-{form}"
    assert (await owner_client.post("/api/repos/create", json={"type": "model", "name": name})).status_code == 200
    base = f"/api/models/owner/{name}"
    await owner_client.post(f"{base}/commit/main", content=_commit_lines("a.txt", "a"), headers=ND)
    await owner_client.post(f"{base}/commit/main", content=_commit_lines("b.txt", "b"), headers=ND)
    squashed = await owner_client.post(f"{base}/super-squash/main", json={"message": "squash"})
    assert squashed.status_code == 200, squashed.text
    oid = squashed.json()["commitOid"]
    head = (await owner_client.get(f"{base}/revision/main")).json()["sha"]
    assert len(oid) == len(head) == (64 if form == "full" else 40)
    assert head.startswith(oid) and oid.startswith(head)
