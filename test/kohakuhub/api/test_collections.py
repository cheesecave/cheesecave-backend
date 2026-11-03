"""Collections: the Hugging Face compatible ``/api/collections`` routes, on real SQL.

A small app with only this router, the real token authentication, and rows that are really committed (the routes
run on other threads). Every shape asserted here comes from Hugging Face's OpenAPI spec and ``huggingface_hub``
(read 2026-11-02); the comments say where a detail is not obvious.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kohakuhub.api import collections as collections_api
from kohakuhub.auth.utils import hash_token
from kohakuhub.config import cfg
from kohakuhub.db import (
    Collection,
    CollectionItem,
    CollectionUpvote,
    RepositoryLike,
    RepositoryMetadata,
    UserOrganization,
)
from test.kohakuhub.support.factories import make_commit, make_org, make_repo, make_token, make_user

THEMES = {"orange", "blue", "green", "purple", "pink", "indigo"}
SLUG = re.compile(r"^(?P<owner>[^/]+)/(?P<title>[^/]+)-(?P<oid>[0-9a-f]{24})$")


# ---------------------------------------------------------------- the world

@pytest.fixture
def api(db_committed, monkeypatch):
    monkeypatch.setattr(collections_api, "db", db_committed)     # the routes' transactions on the database of the test
    app = FastAPI()
    app.include_router(collections_api.router, prefix="/api")
    with TestClient(app, follow_redirects=False) as client:
        yield client


def build_world():
    """alice owns repositories and collections; the organization lab has a member, a visitor and an outsider."""
    w = SimpleNamespace()
    w.alice, w.mika, w.guest, w.stranger = (make_user(n) for n in ("alice", "mika", "guest", "stranger"))
    w.lab = make_org("lab", admin=w.alice)
    UserOrganization.create(user=w.mika, organization=w.lab, role="member")
    UserOrganization.create(user=w.guest, organization=w.lab, role="visitor")
    w.tokens = {}
    for user in (w.alice, w.mika, w.guest, w.stranger):
        w.tokens[user.username] = f"token-{user.username}"
        make_token(user, hash_token(w.tokens[user.username]))
    w.model = make_repo(w.alice, "tagger", "model", downloads=120, likes_count=7)
    w.dataset = make_repo(w.alice, "faces", "dataset", downloads=5)
    w.space = make_repo(w.alice, "demo", "space", likes_count=3)
    w.secret = make_repo(w.alice, "wip", "model", private=True)
    w.lab_model = make_repo(w.lab, "ccip", "model")
    for repo, tags, pipeline in ((w.model, ["onnx"], "image-classification"), (w.dataset, ["faces"], None), (w.space, ["gradio"], None)):
        meta = {"tags": tags, **({"pipeline_tag": pipeline} if pipeline else {})}
        RepositoryMetadata.create(repository=repo, metadata=json.dumps(meta), state="ready", main_sha="a" * 40)
    make_commit(w.model, "c" * 40, created_at=datetime(2026, 10, 1, 12, 30, 45, tzinfo=timezone.utc))
    return w


@pytest.fixture
def world(db_committed):
    return build_world()


def auth(world, who):
    return {"Authorization": f"Bearer {world.tokens[who]}"} if who else {}


def create(api, world, who="alice", **body):
    body.setdefault("title", "My collection")
    return api.post("/api/collections", json=body, headers=auth(world, who))


def add(api, world, slug, kind, ident, who="alice", **extra):
    return api.post(f"/api/collections/{slug}/items", json={"item": {"type": kind, "id": ident}, **extra}, headers=auth(world, who))


def made(api, world, who="alice", items=(), **body):
    """A collection through the API, holding ``items`` ((type, id) pairs); returns its JSON."""
    col = create(api, world, who, **body).json()
    for kind, ident in items:
        assert add(api, world, col["slug"], kind, ident, who).status_code == 200
    return api.get(f"/api/collections/{col['slug']}", headers=auth(world, who)).json()


# ---------------------------------------------------------------- small functions

@pytest.mark.parametrize(
    "title, slug",
    [
        ("ICCV 2026", "iccv-2026"),
        ("💧 LFM3", "lfm3"),                       # emoji go, as on Hugging Face
        ("  Many   spaces & symbols!! ", "many-spaces-symbols"),
        ("snake_case title", "snake-case-title"),
        ("数据集 合集", "数据集-合集"),               # letters of any script stay
        ("🎉🎉", "collection"),                    # nothing left: a fixed word, never an empty slug
        ("a" * 60, "a" * 60),
        ("word " * 20, "-".join(["word"] * 12)),             # cut at 60 characters, and no hyphen is left at the end
    ],
)
def test_title_slug(title, slug):
    assert collections_api.title_slug(title) == slug


def test_new_oid_is_24_hex_and_unique():
    oids = {collections_api.new_oid() for _ in range(50)}
    assert len(oids) == 50 and all(re.fullmatch(r"[0-9a-f]{24}", o) for o in oids)


def test_render_note_escapes_html_and_links_addresses():
    note = collections_api.render_note('see https://example.org/a?b=1 <b>bold</b> & "quote"\nnext line')
    assert note["text"] == 'see https://example.org/a?b=1 <b>bold</b> & "quote"\nnext line'
    assert '<a href="https://example.org/a?b=1" rel="noopener nofollow">https://example.org/a?b=1</a>' in note["html"]
    assert "&lt;b&gt;bold&lt;/b&gt;" in note["html"] and "<b>" not in note["html"] and "<br>next line" in note["html"]


def test_iso_is_utc_with_milliseconds_and_a_z():
    assert collections_api._iso(datetime(2026, 1, 2, 3, 4, 5, 678000)) == "2026-01-02T03:04:05.678Z"            # naive is UTC
    assert collections_api._iso(datetime(2026, 1, 2, 4, 4, 5, tzinfo=timezone(timedelta(hours=1)))) == "2026-01-02T03:04:05.000Z"


# ---------------------------------------------------------------- creating

def test_create_answers_the_collection_a_client_expects(api, world):
    response = create(api, world, description="about it")
    assert response.status_code == 200
    col = response.json()
    match = SLUG.match(col["slug"])
    assert match and match["owner"] == "alice" and match["title"] == "my-collection"
    assert col["title"] == "My collection" and col["description"] == "about it" and col["items"] == []
    assert col["private"] is False and col["theme"] in THEMES and col["upvotes"] == 0 and col["position"] == 0
    assert col["isUpvotedByUser"] is False and col["gating"] is False
    assert col["owner"]["name"] == "alice" and col["owner"]["type"] == "user" and col["owner"]["isPro"] is False
    assert re.fullmatch(r"[0-9a-f]{24}", col["owner"]["_id"])
    assert col["shareUrl"] == f"{cfg.app.base_url.rstrip('/')}/collections/alice/my-collection"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", col["lastUpdated"])


def test_create_in_an_organization_and_the_owner_is_an_org_without_isPro(api, world):
    col = create(api, world, "mika", namespace="lab", private=True).json()
    assert col["owner"]["type"] == "org" and "isPro" not in col["owner"] and col["private"] is True


def test_positions_count_up_per_owner(api, world):
    first, second = create(api, world, title="One").json(), create(api, world, title="Two").json()
    other = create(api, world, "mika", title="Three").json()
    assert (first["position"], second["position"], other["position"]) == (0, 1, 0)


def test_create_with_a_first_item_and_an_invalid_one_creates_nothing(api, world):
    col = create(api, world, item={"type": "model", "id": "alice/tagger"}).json()
    assert [i["id"] for i in col["items"]] == ["alice/tagger"]
    missing = create(api, world, title="Never", item={"type": "model", "id": "alice/missing"})
    assert missing.status_code == 404
    assert Collection.select().where(Collection.title == "Never").count() == 0     # rolled back with its item


@pytest.mark.parametrize(
    "body, why",
    [
        ({"title": ""}, "title"),
        ({"title": "x" * 61}, "title"),
        ({"title": "ok", "description": "d" * 151}, "description"),
        ({"description": "no title"}, "title is required"),
        ({"title": 7}, "title is required"),
    ],
)
def test_create_refuses_bad_text(api, world, body, why):
    response = api.post("/api/collections", json=body, headers=auth(world, "alice"))
    assert response.status_code == 400 and why in response.json()["error"]


def test_create_needs_a_user_and_the_right_to_write_in_the_namespace(api, world):
    assert create(api, world, None).status_code == 401
    assert create(api, world, "mika", namespace="alice").status_code == 403           # someone else's namespace
    assert create(api, world, "guest", namespace="lab").status_code == 403            # a visitor of the organization
    assert create(api, world, "stranger", namespace="lab").status_code == 403
    assert create(api, world, namespace="nobody").status_code == 403                  # no such namespace


def test_resource_groups_are_501_not_ignored(api, world):
    # An Enterprise feature of Hugging Face that CheeseCave does not have: refuse it instead of pretending to apply it
    response = create(api, world, title="Grouped", resourceGroupId="66980ecfc1e12a49c8f0e42d")
    assert response.status_code == 501 and response.headers["x-error-code"] == "NotImplemented"
    assert "resource group" in response.json()["error"]
    assert Collection.select().where(Collection.title == "Grouped").count() == 0
    assert create(api, world, title="Not grouped", resourceGroupId=None).status_code == 200      # null means none: the client sends it so
    col = made(api, world, title="Own")
    url = f"/api/collections/{col['slug']}/resource-group"
    assert api.post(url, json={"resourceGroupId": "66980ecfc1e12a49c8f0e42d"}, headers=auth(world, "alice")).status_code == 501
    assert api.post(url, json={"resourceGroupId": None}, headers=auth(world, "alice")).status_code == 200        # taking it out of none: nothing to do
    assert api.post(url, json={}, headers={}).status_code == 401
    assert api.post(url, json={}, headers=auth(world, "mika")).status_code == 403                               # not the owner
    assert api.post(f"/api/collections/alice/missing-{'0' * 24}/resource-group", json={}, headers=auth(world, "alice")).status_code == 404


def test_the_same_title_slug_is_409_with_the_slug_of_the_existing_one(api, world):
    first = create(api, world, title="Same Title").json()
    second = create(api, world, title="same-title!")
    assert second.status_code == 409
    assert second.json() == {"error": "The collection already exists", "slug": first["slug"]}
    assert create(api, world, "mika", title="Same Title").status_code == 200      # another owner may


def test_an_owner_can_have_only_so_many_collections(api, world, monkeypatch):
    monkeypatch.setattr(cfg.app, "collection_max_per_owner", 2)
    assert [create(api, world, title=t).status_code for t in ("A", "B", "C")] == [200, 200, 400]
    monkeypatch.setattr(cfg.app, "collection_max_per_owner", 0)                       # 0 = no limit
    assert create(api, world, title="D").status_code == 200


# ---------------------------------------------------------------- reading

def test_get_by_the_canonical_slug_the_short_slug_and_an_upper_case_id(api, world):
    col = made(api, world, title="Find Me")
    canonical = api.get(f"/api/collections/{col['slug']}")
    assert canonical.status_code == 200 and canonical.headers["etag"].startswith('W/"')
    assert api.get("/api/collections/alice/find-me").json()["slug"] == col["slug"]
    upper = col["slug"].rsplit("-", 1)[0] + "-" + col["slug"].rsplit("-", 1)[1].upper()
    assert api.get(f"/api/collections/{upper}").json()["slug"] == col["slug"]


def test_another_title_in_front_of_the_same_id_is_307_to_the_canonical_slug(api, world):
    col = made(api, world, title="Real Title")
    oid = SLUG.match(col["slug"])["oid"]
    for method, suffix in (("get", ""), ("patch", ""), ("post", "/items"), ("delete", "/items/" + "0" * 24)):
        response = getattr(api, method)(f"/api/collections/alice/old-name-{oid}{suffix}", headers=auth(world, "alice"))
        assert response.status_code == 307
        assert response.headers["location"] == f"/api/collections/{col['slug']}{suffix}"


@pytest.mark.parametrize("path", ["bob/real-title-{oid}", "alice/{oid}", "alice/real-title-{short}", "alice/no-such-title"])
def test_other_addresses_are_404(api, world, path):
    col = made(api, world, title="Real Title")
    oid = SLUG.match(col["slug"])["oid"]
    response = api.get("/api/collections/" + path.format(oid=oid, short=oid[:-1]))
    assert response.status_code == 404 and response.json() == {"error": "Collection not found"}


def test_a_private_collection_is_only_found_by_those_who_may_see_it(api, world):
    col = made(api, world, "mika", namespace="lab", private=True, title="Secret Lab")
    seen = {who: api.get(f"/api/collections/{col['slug']}", headers=auth(world, who)).status_code
            for who in (None, "stranger", "alice", "mika", "guest")}
    assert seen == {None: 404, "stranger": 404, "alice": 200, "mika": 200, "guest": 200}   # the visitor reads, as for repositories
    personal = made(api, world, title="Mine", private=True)
    assert [api.get(f"/api/collections/{personal['slug']}", headers=auth(world, w)).status_code for w in ("alice", "mika", None)] == [200, 404, 404]


def test_items_have_the_shape_of_their_type_in_the_spec(api, world):
    other = made(api, world, title="Child")
    col = made(api, world, items=[("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo"), ("collection", other["slug"])], title="Shapes")
    model, dataset, space, child = col["items"]
    assert [i["position"] for i in col["items"]] == [0, 1, 2, 3] and all(re.fullmatch(r"[0-9a-f]{24}", i["_id"]) for i in col["items"])
    assert model == {
        "_id": model["_id"], "position": 0, "type": "model", "author": "alice", "id": "alice/tagger", "repoType": "model",
        "private": False, "likes": 7, "downloads": 120, "gated": False, "isLikedByUser": False,
        "lastModified": "2026-10-01T12:30:45.000Z", "availableInferenceProviders": [], "pipeline_tag": "image-classification",
    }
    assert "tags" not in model and "tags" not in dataset                       # the variants are closed: only a Space has tags
    assert dataset["downloads"] == 5 and dataset["gated"] is False and "availableInferenceProviders" not in dataset
    assert "downloads" not in space and "gated" not in space                   # a Space has neither
    assert space["tags"] == ["gradio"] and space["visibility"] == "public" and space["pinned"] is False
    assert space["featured"] is False and space["disabled"] is False and space["emoji"] and space["title"] == "demo"
    assert space["runtime"] == {"stage": "STOPPED", "hardware": {"current": None, "requested": None}, "replicas": {"current": None, "requested": 1}}
    assert child["type"] == "collection" and child["slug"] == child["id"] == other["slug"] and child["title"] == "Child"
    assert child["numberItems"] == 0 and child["owner"]["name"] == "alice" and "description" not in child


def _set_metadata(repo, value):
    meta = RepositoryMetadata.get(RepositoryMetadata.repository == repo)
    meta.metadata = json.dumps(value)
    meta.save()


@pytest.mark.parametrize(
    "card, pipeline",
    [
        ({"pipeline_tag": "text-to-image"}, "text-to-image"),
        ({"pipeline_tag": ["text-to-image", "other"]}, "text-to-image"),     # a card written as a list (seen on a real model)
        ({"pipeline_tag": []}, None),
        ({"pipeline_tag": [3]}, None),
        ({"pipeline_tag": {"a": 1}}, None),
        ({"pipeline_tag": 5}, None),
        ({"pipeline_tag": ""}, None),
    ],
)
def test_a_model_card_of_any_shape_gives_a_pipeline_tag_that_is_a_string_or_none(api, world, card, pipeline):
    _set_metadata(world.model, card)
    col = made(api, world, items=[("model", "alice/tagger")], title="Odd model")
    item = col["items"][0]
    assert item.get("pipeline_tag") == pipeline and ("pipeline_tag" in item) is (pipeline is not None)


@pytest.mark.parametrize(
    "card, tags, emoji, title, color",
    [
        ({"tags": "gradio", "emoji": 5, "title": ["x"], "colorFrom": None}, [], "🧀", "demo", "gray"),
        ({"tags": ["a", 3, None, "b"], "emoji": "🚀", "title": "Nice", "colorFrom": "red"}, ["a", "b"], "🚀", "Nice", "red"),
        ({"tags": {"a": 1}}, [], "🧀", "demo", "gray"),
    ],
)
def test_a_space_card_of_any_shape_gives_the_fields_the_spec_wants(api, world, card, tags, emoji, title, color):
    _set_metadata(world.space, card)
    col = made(api, world, items=[("space", "alice/demo")], title="Odd space")
    item = col["items"][0]
    assert (item["tags"], item["emoji"], item["title"], item["colorFrom"]) == (tags, emoji, title, color)
    assert item["colorTo"] == "gray"


def test_the_viewer_sees_their_own_like_and_a_space_keeps_its_metadata(api, world):
    RepositoryLike.create(repository=world.model, user=world.mika)
    meta = RepositoryMetadata.get(RepositoryMetadata.repository == world.space)
    meta.metadata = json.dumps({"tags": [], "emoji": "🚀", "colorFrom": "red", "colorTo": "blue", "title": "Nice demo", "sdk": "gradio"})
    meta.save()
    col = made(api, world, items=[("model", "alice/tagger"), ("space", "alice/demo")], title="Likes")
    as_mika = api.get(f"/api/collections/{col['slug']}", headers=auth(world, "mika")).json()
    assert as_mika["items"][0]["isLikedByUser"] is True and api.get(f"/api/collections/{col['slug']}").json()["items"][0]["isLikedByUser"] is False
    space = as_mika["items"][1]
    assert (space["emoji"], space["colorFrom"], space["colorTo"], space["title"], space["sdk"]) == ("🚀", "red", "blue", "Nice demo", "gradio")


def test_a_repository_without_a_commit_or_metadata_still_has_a_last_modified(api, world):
    make_repo(world.alice, "bare", "model")
    col = made(api, world, items=[("model", "alice/bare")], title="Bare")
    assert col["items"][0]["lastModified"].endswith("Z") and "pipeline_tag" not in col["items"][0]


def test_notes_come_as_text_and_html(api, world):
    col = made(api, world, title="Notes")
    add(api, world, col["slug"], "model", "alice/tagger", note="see https://example.org <i>x</i>")
    note = api.get(f"/api/collections/{col['slug']}").json()["items"][0]["note"]
    assert note["text"] == "see https://example.org <i>x</i>" and "&lt;i&gt;" in note["html"] and 'href="https://example.org"' in note["html"]
    add(api, world, col["slug"], "dataset", "alice/faces")
    assert "note" not in api.get(f"/api/collections/{col['slug']}").json()["items"][1]      # no note, no key


def test_items_are_filtered_for_the_viewer(api, world):
    private_child = made(api, world, title="Hidden child", private=True)
    col = made(api, world, items=[("model", "alice/tagger"), ("model", "alice/wip"), ("collection", private_child["slug"]), ("model", "lab/ccip")], title="Mixed")
    assert [i["id"] for i in api.get(f"/api/collections/{col['slug']}", headers=auth(world, "alice")).json()["items"]] == [
        "alice/tagger", "alice/wip", private_child["slug"], "lab/ccip"]
    assert [i["id"] for i in api.get(f"/api/collections/{col['slug']}").json()["items"]] == ["alice/tagger", "lab/ccip"]
    lab_private = made(api, world, "mika", namespace="lab", private=True, title="Lab private")
    nested = made(api, world, title="Holds lab private", items=[("collection", lab_private["slug"])])
    assert api.get(f"/api/collections/{nested['slug']}", headers=auth(world, "guest")).json()["items"][0]["numberItems"] == 0
    child_view = lambda who: api.get(f"/api/collections/{col['slug']}", headers=auth(world, who)).json()["items"]
    assert len(child_view("mika")) == 2 and len(child_view("alice")) == 4


# ---------------------------------------------------------------- updating a collection

def test_patch_answers_success_and_the_collection_under_data(api, world):
    col = made(api, world, title="Old Title", description="old")
    response = api.patch(f"/api/collections/{col['slug']}", json={"title": "New Title", "description": "new", "theme": "pink", "private": True}, headers=auth(world, "alice"))
    assert response.status_code == 200 and response.json()["success"] is True
    data = response.json()["data"]
    assert (data["title"], data["description"], data["theme"], data["private"]) == ("New Title", "new", "pink", True)
    assert data["slug"].startswith("alice/new-title-") and data["slug"].endswith(SLUG.match(col["slug"])["oid"])     # the id stays
    assert api.get(f"/api/collections/{col['slug']}", headers=auth(world, "alice")).status_code == 307             # the old slug is moved


def test_patch_refuses_what_is_not_allowed(api, world):
    col = made(api, world, title="Patch Me")
    other = made(api, world, title="Other")
    url, headers = f"/api/collections/{col['slug']}", auth(world, "alice")
    assert api.patch(url, json={"theme": "red"}, headers=headers).status_code == 400
    assert api.patch(url, json={"title": ""}, headers=headers).status_code == 400
    assert api.patch(url, json={"description": "d" * 151}, headers=headers).status_code == 400
    clash = api.patch(url, json={"title": "other"}, headers=headers)
    assert clash.status_code == 409 and clash.json()["slug"] == other["slug"]
    assert api.patch(url, json={"title": "Patch Me!!"}, headers=headers).status_code == 200      # its own slug is no clash
    assert api.patch(url, json={"private": False}, headers=auth(world, "stranger")).status_code == 403
    assert api.patch(url, json={}, headers={}).status_code == 401
    assert api.patch(f"/api/collections/alice/missing-{'0' * 24}", json={}, headers=headers).status_code == 404


def test_a_visitor_of_the_organization_reads_but_cannot_change(api, world):
    col = made(api, world, "mika", namespace="lab", title="Lab list")
    for method, path, body in (("patch", "", {"title": "x"}), ("delete", "", None), ("post", "/items", {"item": {"type": "model", "id": "lab/ccip"}}),
                               ("post", "/items/batch", []), ("patch", "/items/" + "0" * 24, {}), ("delete", "/items/" + "0" * 24, None)):
        response = getattr(api, method)(f"/api/collections/{col['slug']}{path}", **({"json": body} if body is not None else {}), headers=auth(world, "guest"))
        assert response.status_code == 403, (method, path)
    assert api.patch(f"/api/collections/{col['slug']}", json={"title": "Renamed by a member"}, headers=auth(world, "mika")).status_code == 200


@pytest.mark.parametrize("role, may_write", [("visitor", False), ("member", True), ("admin", True), ("super-admin", True)])
def test_every_role_of_an_organization_reads_and_only_the_writing_roles_write(api, world, role, may_write):
    person = make_user(f"role-{role}")
    make_token(person, hash_token(f"token-{role}"))
    UserOrganization.create(user=person, organization=world.lab, role=role)
    world.tokens[person.username] = f"token-{role}"
    col = made(api, world, "alice", namespace="lab", title="Roles", private=True)
    who = person.username
    assert api.get(f"/api/collections/{col['slug']}", headers=auth(world, who)).status_code == 200      # a private one, too
    assert (api.patch(f"/api/collections/{col['slug']}", json={"theme": "pink"}, headers=auth(world, who)).status_code == 200) is may_write
    assert (create(api, world, who, namespace="lab", title=f"By {role}").status_code == 200) is may_write


def test_text_at_the_largest_size_is_accepted(api, world):
    col = create(api, world, title="t" * 60, description="d" * 150)
    assert col.status_code == 200
    slug = col.json()["slug"]
    assert add(api, world, slug, "model", "alice/tagger", note="n" * 500).status_code == 200
    item = api.get(f"/api/collections/{slug}").json()["items"][0]["_id"]
    assert api.patch(f"/api/collections/{slug}/items/{item}", json={"note": "m" * 500}, headers=auth(world, "alice")).status_code == 200
    assert api.patch(f"/api/collections/{slug}", json={"title": "u" * 60, "description": "e" * 150}, headers=auth(world, "alice")).status_code == 200


def test_moving_a_collection_in_the_list_of_its_owner(api, world):
    cols = [create(api, world, title=t).json() for t in ("A", "B", "C")]
    api.patch(f"/api/collections/{cols[2]['slug']}", json={"position": 0}, headers=auth(world, "alice"))
    order = lambda: [c["title"] for c in api.get("/api/collections?owner=alice&sort=position").json()]
    assert order() == ["C", "A", "B"]
    api.patch(f"/api/collections/{cols[2]['slug']}", json={"position": 99}, headers=auth(world, "alice"))     # past the end: last
    assert order() == ["A", "B", "C"]
    assert [c.position for c in Collection.select().order_by(Collection.position)] == [0, 1, 2]
    assert api.patch(f"/api/collections/{cols[0]['slug']}", json={"position": -1}, headers=auth(world, "alice")).status_code == 400
    assert api.patch(f"/api/collections/{cols[0]['slug']}", json={"position": "1"}, headers=auth(world, "alice")).status_code == 400


def test_delete_a_collection_takes_its_items_and_upvotes_along(api, world):
    col = made(api, world, items=[("model", "alice/tagger")], title="Doomed")
    api.post(f"/api/collections/{col['slug']}/upvote", headers=auth(world, "mika"))
    assert api.delete(f"/api/collections/{col['slug']}", headers=auth(world, "stranger")).status_code == 403
    response = api.delete(f"/api/collections/{col['slug']}", headers=auth(world, "alice"))
    assert response.status_code == 204 and response.content == b""
    assert (Collection.select().count(), CollectionItem.select().count(), CollectionUpvote.select().count()) == (0, 0, 0)
    assert api.delete(f"/api/collections/{col['slug']}", headers=auth(world, "alice")).status_code == 404


# ---------------------------------------------------------------- items

def test_add_the_four_kinds_of_item_and_new_ones_go_last(api, world):
    other = made(api, world, title="Other")
    col = create(api, world, title="Kinds").json()
    for kind, ident in (("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo"), ("collection", other["slug"])):
        response = add(api, world, col["slug"], kind, ident)
        assert response.status_code == 200 and response.json()["items"][-1]["type"] == kind
    assert [i["position"] for i in response.json()["items"]] == [0, 1, 2, 3]


def test_add_refuses_what_is_not_an_item(api, world):
    col = create(api, world).json()
    url, headers = f"/api/collections/{col['slug']}/items", auth(world, "alice")
    for item in (None, {}, {"type": "model"}, {"type": "model", "id": ""}, {"type": "model", "id": 5}, {"type": "folder", "id": "a/b"}):
        response = api.post(url, json={"item": item}, headers=headers)
        assert response.status_code == 400, item
    assert api.post(url, json={"item": {"type": "model", "id": "alice/tagger"}, "note": "n" * 501}, headers=headers).status_code == 400
    assert api.post(url, json={"item": {"type": "model", "id": "alice/tagger"}}, headers={}).status_code == 401


@pytest.mark.parametrize("kind", ["paper", "bucket"])
def test_papers_and_buckets_are_501_with_a_message(api, world, kind):
    col = create(api, world).json()
    response = add(api, world, col["slug"], kind, "2307.09288")
    assert response.status_code == 501 and kind in response.json()["error"] and response.headers["x-error-message"]


def test_add_what_cannot_be_found_or_seen_is_404(api, world):
    col = create(api, world, "mika").json()
    for kind, ident in (("model", "alice/missing"), ("model", "noslash"), ("dataset", "alice/tagger"), ("model", "alice/wip"), ("collection", "alice/nothing-" + "0" * 24)):
        assert add(api, world, col["slug"], kind, ident, "mika").status_code == 404, (kind, ident)       # a private repository does not leak
    hidden = made(api, world, title="Not for Mika", private=True)
    assert add(api, world, col["slug"], "collection", hidden["slug"], "mika").status_code == 404


def test_a_collection_cannot_hold_itself_or_the_same_item_twice(api, world):
    col, other = create(api, world, title="Self").json(), made(api, world, title="Other")
    assert add(api, world, col["slug"], "collection", col["slug"]).status_code == 400
    for kind, ident in (("model", "alice/tagger"), ("collection", other["slug"])):
        assert add(api, world, col["slug"], kind, ident).status_code == 200
        duplicate = add(api, world, col["slug"], kind, ident)
        assert duplicate.status_code == 409 and "already" in duplicate.json()["error"]
    assert add(api, world, other["slug"], "collection", col["slug"]).status_code == 200      # a loop is harmless: never expanded


def test_the_same_item_added_a_moment_ago_by_another_request_is_409(api, world, monkeypatch):
    col = made(api, world, title="Double add")
    real_oid = collections_api.new_oid

    def other_request_lands_first():       # between the check for the item and its insert
        monkeypatch.setattr(collections_api, "new_oid", real_oid)
        CollectionItem.create(
            oid=real_oid(), collection=Collection.get(Collection.oid == col["slug"][-24:]), item_type="model",
            repository=world.model, position=0,
        )
        return real_oid()

    monkeypatch.setattr(collections_api, "new_oid", other_request_lands_first)
    response = add(api, world, col["slug"], "model", "alice/tagger")
    assert response.status_code == 409 and "already" in response.json()["error"]


def test_a_collection_can_hold_only_so_many_items(api, world, monkeypatch):
    monkeypatch.setattr(cfg.app, "collection_max_items", 2)
    col = create(api, world).json()
    assert [add(api, world, col["slug"], k, i).status_code for k, i in (("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo"))] == [200, 200, 400]
    monkeypatch.setattr(cfg.app, "collection_max_items", 0)
    assert add(api, world, col["slug"], "space", "alice/demo").status_code == 200


def test_positions_have_gaps_after_a_delete_and_new_items_go_after_the_highest(api, world):
    col = made(api, world, items=[("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo")], title="Gaps")
    api.delete(f"/api/collections/{col['slug']}/items/{col['items'][1]['_id']}", headers=auth(world, "alice"))
    now = api.get(f"/api/collections/{col['slug']}").json()["items"]
    assert [i["position"] for i in now] == [0, 2]                                  # no renumbering
    added = add(api, world, col["slug"], "model", "lab/ccip").json()["items"]
    assert [i["position"] for i in added] == [0, 2, 3]


def test_update_an_item_note_position_and_clear_the_note(api, world):
    col = made(api, world, items=[("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo")], title="Edit")
    ids = [i["_id"] for i in col["items"]]
    url = lambda n: f"/api/collections/{col['slug']}/items/{ids[n]}"
    headers = auth(world, "alice")
    assert api.patch(url(2), json={"note": "front", "position": 0, "gallery": ["https://example.org/a.png"]}, headers=headers).status_code == 200
    items = api.get(f"/api/collections/{col['slug']}").json()["items"]
    assert [i["_id"] for i in items] == [ids[2], ids[0], ids[1]] and [i["position"] for i in items] == [0, 1, 2]
    assert items[0]["note"]["text"] == "front"
    api.patch(url(2), json={"note": ""}, headers=headers)
    assert "note" not in api.get(f"/api/collections/{col['slug']}").json()["items"][0]
    api.patch(url(0), json={"position": 99}, headers=headers)                       # past the end: last
    assert api.get(f"/api/collections/{col['slug']}").json()["items"][-1]["_id"] == ids[0]


def test_update_an_item_refuses_bad_values_and_missing_items(api, world):
    col = made(api, world, items=[("model", "alice/tagger")], title="Bad")
    url, headers, item = f"/api/collections/{col['slug']}/items", auth(world, "alice"), col["items"][0]["_id"]
    assert api.patch(f"{url}/{item}", json={"note": "n" * 501}, headers=headers).status_code == 400
    for position in (-1, "1", 1.5):
        assert api.patch(f"{url}/{item}", json={"position": position}, headers=headers).status_code == 400
    assert api.patch(f"{url}/{'0' * 24}", json={"note": "x"}, headers=headers).status_code == 404
    assert api.patch(f"{url}/{item}", json={"note": "x"}, headers=auth(world, "stranger")).status_code == 403
    assert api.patch(f"{url}/{item.upper()}", json={"note": "upper"}, headers=headers).status_code == 200   # the id is hex: either case


def test_batch_updates_are_all_or_nothing(api, world):
    col = made(api, world, items=[("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo")], title="Batch")
    ids = [i["_id"] for i in col["items"]]
    url, headers = f"/api/collections/{col['slug']}/items/batch", auth(world, "alice")
    ok = api.post(url, json=[{"action": "update", "_id": ids[0], "data": {"note": "one"}}, {"action": "update", "_id": ids[2], "data": {"position": 0}}], headers=headers)
    assert ok.status_code == 200
    items = api.get(f"/api/collections/{col['slug']}").json()["items"]
    assert items[0]["_id"] == ids[2] and items[1]["note"]["text"] == "one"
    before = api.get(f"/api/collections/{col['slug']}").json()["items"]
    for body, status in (
        ({"not": "a list"}, 400),
        ([{"action": "delete", "_id": ids[0], "data": {}}], 400),
        (["x"], 400),
        ([{"action": "update", "_id": ids[0], "data": {"note": "never"}}, {"action": "update", "_id": "f" * 24, "data": {}}], 404),
        ([{"action": "update", "_id": ids[0], "data": {"note": "never"}}, {"action": "update", "_id": ids[1], "data": {"position": -5}}], 400),
        ([{"action": "update", "_id": ids[0]}], 200),                                         # no data: nothing to change
    ):
        assert api.post(url, json=body, headers=headers).status_code == status, body
    assert api.get(f"/api/collections/{col['slug']}").json()["items"] == before            # the failed ones changed nothing


def test_delete_an_item(api, world):
    col = made(api, world, items=[("model", "alice/tagger")], title="Drop")
    url = f"/api/collections/{col['slug']}/items/{col['items'][0]['_id']}"
    assert api.delete(url, headers=auth(world, "stranger")).status_code == 403
    assert api.delete(url, headers=auth(world, "alice")).status_code == 204
    assert api.delete(url, headers=auth(world, "alice")).status_code == 404
    assert api.get(f"/api/collections/{col['slug']}").json()["items"] == []


def test_a_deleted_repository_leaves_its_collections(api, world):
    col = made(api, world, items=[("model", "alice/tagger"), ("dataset", "alice/faces")], title="Gone")
    world.model.delete_instance()      # the rows of the items leave with it, by ON DELETE CASCADE
    assert [i["id"] for i in api.get(f"/api/collections/{col['slug']}").json()["items"]] == ["alice/faces"]


def test_a_renamed_repository_stays_in_its_collections(api, world):
    col = made(api, world, items=[("model", "alice/tagger")], title="Renamed")
    world.model.name, world.model.full_id = "tagger-v2", "alice/tagger-v2"
    world.model.save()
    assert api.get(f"/api/collections/{col['slug']}").json()["items"][0]["id"] == "alice/tagger-v2"


# ---------------------------------------------------------------- upvotes

def test_upvote_counts_once_and_can_be_taken_back(api, world):
    col = made(api, world, title="Up")
    url = f"/api/collections/{col['slug']}/upvote"
    assert api.post(url).status_code == 401
    assert api.post(url, headers=auth(world, "mika")).json() == {"success": True, "upvotes": 1}
    assert api.post(url, headers=auth(world, "mika")).json()["upvotes"] == 1                  # twice is still once
    api.post(url, headers=auth(world, "guest"))
    assert api.get(f"/api/collections/{col['slug']}", headers=auth(world, "mika")).json()["isUpvotedByUser"] is True
    assert api.get(f"/api/collections/{col['slug']}").json()["upvotes"] == 2
    assert api.delete(url, headers=auth(world, "mika")).json() == {"success": True, "upvotes": 1}
    assert api.delete(url, headers=auth(world, "mika")).json()["upvotes"] == 1                # nothing to take back
    assert api.get(f"/api/collections/{col['slug']}", headers=auth(world, "mika")).json()["isUpvotedByUser"] is False


def test_a_second_upvote_of_the_same_moment_is_still_one_upvote(api, world, monkeypatch):
    col = made(api, world, title="Double click")
    url = f"/api/collections/{col['slug']}/upvote"
    assert api.post(url, headers=auth(world, "mika")).json()["upvotes"] == 1
    real, calls = collections_api._upvoted, []

    def not_yet(*args):                    # the check of the second request ran before the first one's upvote was visible
        calls.append(1)
        return False if len(calls) == 1 else real(*args)

    monkeypatch.setattr(collections_api, "_upvoted", not_yet)
    assert api.post(url, headers=auth(world, "mika")).json() == {"success": True, "upvotes": 1}
    assert len(calls) == 1


def test_upvoting_a_collection_one_cannot_see_is_404(api, world):
    col = made(api, world, title="Hidden", private=True)
    url = f"/api/collections/{col['slug']}/upvote"
    assert api.post(url, headers=auth(world, "mika")).status_code == 404
    assert api.delete(url, headers=auth(world, "mika")).status_code == 404


# ---------------------------------------------------------------- listing

@pytest.fixture
def library(api, world):
    """Several collections with known upvotes and ages."""
    now = datetime.now(timezone.utc)
    rows = {}
    for title, owner, votes, age, kwargs in (
        ("Alpha Vision", "alice", 3, 1, {"description": "image models"}),
        ("Beta Text", "alice", 0, 5, {}),
        ("Gamma Audio", "mika", 5, 3, {"description": "SOUND things"}),
        ("Delta Private", "alice", 9, 2, {"private": True}),
        ("Epsilon Lab", "mika", 1, 4, {"namespace": "lab"}),
    ):
        items = [("model", "alice/tagger"), ("dataset", "alice/faces"), ("space", "alice/demo"), ("model", "lab/ccip")]
        col = made(api, world, owner, title=title, items=items + ([("model", "alice/wip")] if owner == "alice" else []), **kwargs)
        rows[title] = col
        Collection.update(updated_at=now - timedelta(days=age), upvotes_count=votes).where(Collection.oid == SLUG.match(col["slug"])["oid"]).execute()
    voters = [world.alice, world.mika, world.guest, world.stranger]
    for title, count, days_ago in (("Beta Text", 3, 1), ("Alpha Vision", 1, 20), ("Epsilon Lab", 2, 2)):      # recent upvotes drive "trending"
        oid = SLUG.match(rows[title]["slug"])["oid"]
        for voter in voters[:count]:
            CollectionUpvote.create(collection=Collection.get(Collection.oid == oid), user=voter, created_at=now - timedelta(days=days_ago))
    return rows


def titles(response):
    assert response.status_code == 200, response.text
    return [c["title"] for c in response.json()]


def test_the_default_is_trending_then_the_latest_change(api, library):
    # recent upvotes (last 7 days): Beta 3, Epsilon 2, the rest 0 (Alpha's one is 20 days old); ties by last change
    assert titles(api.get("/api/collections")) == ["Beta Text", "Epsilon Lab", "Alpha Vision", "Gamma Audio"]
    assert titles(api.get("/api/collections?sort=trending")) == titles(api.get("/api/collections"))


def test_sorting_by_upvotes_last_change_and_the_owners_own_order(api, library, world):
    assert titles(api.get("/api/collections?sort=upvotes")) == ["Gamma Audio", "Alpha Vision", "Epsilon Lab", "Beta Text"]
    assert titles(api.get("/api/collections?sort=lastModified")) == ["Alpha Vision", "Gamma Audio", "Epsilon Lab", "Beta Text"]
    assert titles(api.get("/api/collections?sort=position&owner=alice", headers=auth(world, "alice"))) == ["Alpha Vision", "Beta Text", "Delta Private"]
    assert titles(api.get("/api/collections?sort=position")) == ["Alpha Vision", "Beta Text", "Epsilon Lab", "Gamma Audio"]   # by owner name, then position


def test_visibility_follows_the_viewer(api, library, world):
    everyone = lambda who: set(titles(api.get("/api/collections?limit=100", headers=auth(world, who))))
    public = {"Alpha Vision", "Beta Text", "Gamma Audio", "Epsilon Lab"}
    assert everyone(None) == everyone("stranger") == public
    assert everyone("alice") == public | {"Delta Private"}
    assert everyone("mika") == public                                                        # Delta is alice's own
    assert everyone("guest") == public


def test_a_private_collection_of_an_organization_is_listed_for_its_members_and_visitors(api, world):
    made(api, world, "mika", namespace="lab", private=True, title="Lab only")
    seen = {who: titles(api.get("/api/collections", headers=auth(world, who))) for who in (None, "stranger", "alice", "mika", "guest")}
    assert seen == {None: [], "stranger": [], "alice": ["Lab only"], "mika": ["Lab only"], "guest": ["Lab only"]}


def test_a_list_entry_is_what_huggingface_hub_builds_a_collection_from(api, library, world):
    entry = api.get("/api/collections?owner=alice&limit=1").json()[0]
    # huggingface_hub passes position= itself, so an entry must not carry one (nor a share URL); a single collection has both
    assert "position" not in entry and "shareUrl" not in entry
    assert {"slug", "title", "owner", "items", "lastUpdated", "private", "theme", "upvotes", "isUpvotedByUser", "gating"} <= set(entry)
    assert len(entry["items"]) == 4                                                              # a list shows the first four
    single = api.get(f"/api/collections/{entry['slug']}").json()
    assert "position" in single and "shareUrl" in single and len(single["items"]) == 4          # alice/wip is hidden from the viewer
    assert len(api.get(f"/api/collections/{entry['slug']}", headers=auth(world, "alice")).json()["items"]) == 5


def test_filter_by_owner_item_and_text(api, library, world):
    assert titles(api.get("/api/collections?owner=mika")) == ["Gamma Audio"]            # lab is another owner than mika
    assert titles(api.get("/api/collections?owner=mika&owner=lab&sort=lastModified")) == ["Gamma Audio", "Epsilon Lab"]
    assert set(titles(api.get("/api/collections?item=models/alice/tagger&limit=100"))) == {"Alpha Vision", "Beta Text", "Gamma Audio", "Epsilon Lab"}
    assert titles(api.get("/api/collections?item=datasets/alice/faces&item=spaces/alice/demo&sort=lastModified")) == ["Alpha Vision", "Gamma Audio", "Epsilon Lab", "Beta Text"]
    assert titles(api.get("/api/collections?item=papers/2307.09288")) == []                      # there is no paper catalogue
    assert titles(api.get("/api/collections?item=models/alice/nothing")) == []
    assert titles(api.get("/api/collections?q=image")) == ["Alpha Vision"]
    assert titles(api.get("/api/collections?q=SOUND")) == ["Gamma Audio"]                       # case-insensitive, also on SQLite
    assert titles(api.get("/api/collections?q=text")) == ["Beta Text"]
    assert titles(api.get("/api/collections?q=zzz")) == []


def test_several_items_in_the_filter_match_a_collection_with_any_of_them(api, world):
    # as on Hugging Face (tried on its public API: collections of item A + collections of item B = the answer for both)
    only_model = made(api, world, title="Only model", items=[("model", "alice/tagger")])
    only_dataset = made(api, world, title="Only dataset", items=[("dataset", "alice/faces")])
    neither = made(api, world, title="Neither", items=[("space", "alice/demo")])
    both = api.get("/api/collections?item=models/alice/tagger&item=datasets/alice/faces&limit=100").json()
    assert {c["slug"] for c in both} == {only_model["slug"], only_dataset["slug"]}
    assert neither["slug"] not in {c["slug"] for c in both}
    one = api.get("/api/collections?item=models/alice/tagger&limit=100").json()
    assert {c["slug"] for c in one} == {only_model["slug"]}


def test_the_item_filter_does_not_reveal_a_private_repository(api, library, world):
    hit = "/api/collections?item=models/alice/wip&limit=100"
    assert titles(api.get(hit)) == [] == titles(api.get(hit, headers=auth(world, "mika")))
    assert set(titles(api.get(hit, headers=auth(world, "alice")))) == {"Alpha Vision", "Beta Text", "Delta Private"}   # alice's own


@pytest.mark.parametrize(
    "query, message",
    [
        ("sort=newest", 'sort must be one of'),
        ("limit=0", "between 1 and 10000"),
        ("limit=10001", "between 1 and 10000"),
        ("limit=many", "must be a number"),
        ("limit=101", "unless expand is false and owner is set"),
        ("limit=101&expand=false", "unless expand is false and owner is set"),
        ("limit=101&owner=alice", "unless expand is false and owner is set"),
        ("item=collections/x", "item must look like"),
        ("item=models", "item must look like"),
        ("cursor=!!", "invalid cursor"),
        ("cursor=" + "e30", "invalid cursor"),               # valid base64 of {} : no keys
    ],
)
def test_list_refuses_bad_parameters_in_the_error_shape(api, library, query, message):
    response = api.get("/api/collections?" + query)
    assert response.status_code == 400 and message in response.json()["error"]


def test_a_big_limit_is_allowed_for_one_owner_without_the_items(api, library, world):
    response = api.get("/api/collections?owner=alice&expand=false&limit=10000", headers=auth(world, "alice"))
    assert response.status_code == 200
    rows = response.json()
    assert {r["title"] for r in rows} == {"Alpha Vision", "Beta Text", "Delta Private"}
    assert set(rows[0]) == {"name", "private", "title", "upvotes"} and SLUG.match(rows[0]["name"])      # `name` is the full slug
    assert {r["title"]: r["private"] for r in rows}["Delta Private"] is True
    assert api.get("/api/collections?expand=false&limit=5").json()[0].keys() > {"slug", "items"}        # no owner: not the light form


@pytest.mark.parametrize("sort", ["trending", "upvotes", "lastModified", "position"])
def test_paging_by_the_link_header_visits_every_collection_once(api, library, world, sort):
    seen, url = [], f"/api/collections?sort={sort}&limit=2"
    pages = 0
    while url:
        response = api.get(url, headers=auth(world, "alice"))
        assert response.status_code == 200 and len(response.json()) <= 2
        seen += [c["slug"] for c in response.json()]
        link = response.headers.get("link")
        url = re.fullmatch(r'<(?P<url>[^>]+)>; rel="next"', link)["url"] if link else None
        if url:
            assert url.startswith(cfg.app.base_url.rstrip("/") + "/api/collections?") and f"sort={sort}" in url and "limit=2" in url   # absolute, parameters kept
            url = url[len(cfg.app.base_url.rstrip("/")):]
        pages += 1
    assert len(seen) == len(set(seen)) == 5 and pages == 3
    whole = [c["slug"] for c in api.get(f"/api/collections?sort={sort}&limit=100", headers=auth(world, "alice")).json()]
    assert seen == whole                                                                       # the same order, page by page


def test_paging_survives_a_change_between_pages(api, library, world):
    first = api.get("/api/collections?sort=lastModified&limit=2")
    link = re.fullmatch(r'<([^>]+)>; rel="next"', first.headers["link"])[1]
    made(api, world, title="Brand new")                                                          # sorts first; the next page must not repeat or skip
    second = api.get(link[len(cfg.app.base_url.rstrip("/")):])
    assert not {c["slug"] for c in first.json()} & {c["slug"] for c in second.json()}
    assert "Brand new" not in titles(second)


def test_an_exact_page_has_no_link_and_a_forged_cursor_is_refused(api, library):
    assert "link" not in api.get("/api/collections?limit=4").headers                            # four public ones, four asked: no next page
    assert api.get("/api/collections?limit=3").headers["link"]
    import base64
    forged = base64.urlsafe_b64encode(json.dumps({"s": "upvotes", "k": ["x"]}).encode()).decode().rstrip("=")
    assert api.get(f"/api/collections?sort=lastModified&cursor={forged}").status_code == 400    # a cursor of another sort
    garbage = base64.urlsafe_b64encode(json.dumps({"s": "lastModified", "k": ["not-a-date", 1]}).encode()).decode().rstrip("=")
    assert api.get(f"/api/collections?sort=lastModified&cursor={garbage}").status_code == 400
    assert api.get(f"/api/collections?sort=upvotes&cursor={base64.urlsafe_b64encode(json.dumps({'s': 'upvotes', 'k': [1, 'bad', 1]}).encode()).decode()}").status_code == 400


# ---------------------------------------------------------------- bodies that are not what they should be

def test_a_body_that_is_not_json_or_not_an_object_is_refused_or_ignored(api, world):
    headers = {**auth(world, "alice"), "Content-Type": "application/json"}
    assert api.post("/api/collections", content="not json", headers=headers).status_code == 400
    assert api.post("/api/collections", content="[1, 2]", headers=headers).status_code == 400
    col = create(api, world).json()
    url = f"/api/collections/{col['slug']}"
    assert api.patch(url, content="not json", headers=headers).status_code == 200              # nothing to change
    assert api.post(url + "/items", content="not json", headers=headers).status_code == 400    # no item
    assert api.post(url + "/items/batch", content="not json", headers=headers).status_code == 400
    assert api.patch(url + "/items/" + "0" * 24, content="not json", headers=headers).status_code == 404


def test_private_must_be_true_or_false(api, world):
    col = create(api, world).json()
    assert api.patch(f"/api/collections/{col['slug']}", json={"private": "yes"}, headers=auth(world, "alice")).status_code == 400
    assert api.patch(f"/api/collections/{col['slug']}", json={"private": 1}, headers=auth(world, "alice")).status_code == 400


def test_the_same_title_created_a_moment_ago_by_someone_else_is_still_409(api, world, monkeypatch):
    class AppSettings:
        """Reads the limit after the check for an existing slug, which is when the other request lands."""

        def __getattr__(self, name):
            return getattr(cfg.app, name)

        @property
        def collection_max_per_owner(self):
            if not Collection.select().where(Collection.slug == "race").exists():
                Collection.create(oid=collections_api.new_oid(), owner=world.alice, title="Race", slug="race", theme="green")
            return 0

    monkeypatch.setattr(collections_api, "cfg", SimpleNamespace(app=AppSettings()))
    response = create(api, world, title="Race")
    assert response.status_code == 409
    assert response.json()["slug"].startswith("alice/race-")


def test_an_item_is_a_repository_or_a_collection_never_both_or_neither(db_committed, world):
    from peewee import IntegrityError
    col = Collection.create(oid="a" * 24, owner=world.alice, title="Check", slug="check", theme="green")
    other = Collection.create(oid="b" * 24, owner=world.alice, title="Other", slug="other", theme="green")
    for values in ({}, {"repository": world.model, "child": other}):
        with pytest.raises(IntegrityError):
            CollectionItem.create(oid="c" * 24, collection=col, item_type="model", position=0, **values)
