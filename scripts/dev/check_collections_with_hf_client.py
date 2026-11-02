#!/usr/bin/env python3
"""Drive the collections API with the real huggingface_hub client and report what works.

usage: python scripts/dev/check_collections_with_hf_client.py ENDPOINT TOKENS.json [ORG]

TOKENS.json maps the user names alice, mika and guest to API tokens of three users of the instance. The script makes the
repositories it needs under alice (``cc-check-model``, ``-dataset``, ``-space``, ``-private``), and every collection it
creates is deleted at the end. With ORG, an organization where alice is an admin, mika a member and guest a visitor, it also
checks the organization rules. Run it with the version of huggingface_hub to try (0.20.3 is the first with collections).
"""
import json
import re
import secrets
import sys
import traceback
import urllib.request

import huggingface_hub
from huggingface_hub import HfApi

try:
    from huggingface_hub.errors import HfHubHTTPError
except ImportError:       # before 0.26
    from huggingface_hub.utils import HfHubHTTPError

endpoint, tokens = sys.argv[1], json.load(open(sys.argv[2]))
org = sys.argv[3] if len(sys.argv) > 3 else None
alice, mika, guest = (HfApi(endpoint=endpoint, token=tokens[n]) for n in ("alice", "mika", "guest"))
try:
    anon = HfApi(endpoint=endpoint, token=False)    # token=False: no token even if one is saved (newer clients)
except Exception:
    anon = HfApi(endpoint=endpoint)
results, created = [], []
tag = secrets.token_hex(3)
MODEL, DATASET, SPACE, PRIVATE = (f"alice/cc-check-{n}" for n in ("model", "dataset", "space", "private"))


def check(name):
    def wrap(fn):
        try:
            fn()
            results.append((name, True, ""))
        except AssertionError as err:
            results.append((name, False, f"assertion: {err}"))
        except Exception as err:
            results.append((name, False, f"{type(err).__name__}: {str(err)[:160]}"))
            traceback.print_exc(limit=2)
        return fn
    return wrap


def status_of(fn):
    try:
        fn()
        return 200
    except HfHubHTTPError as err:
        return err.response.status_code


def make(api, title, **kwargs):
    col = api.create_collection(f"{title} {tag}", **kwargs)
    created.append((api, col.slug))
    return col


def ensure_repos():
    for repo_id, kind, private in ((MODEL, "model", False), (DATASET, "dataset", False), (SPACE, "space", False), (PRIVATE, "model", True)):
        extra = {"space_sdk": "static"} if kind == "space" else {}
        alice.create_repo(repo_id, repo_type=kind, private=private, exist_ok=True, **extra)


ensure_repos()
state = {}
SLUG = re.compile(r"^alice/check-main-" + tag + r"-[0-9a-f]{24}$")


@check("create_collection returns a Collection with a canonical slug, a theme, no items")
def _():
    col = make(alice, "Check main", description="made by the client check", private=False)
    state["slug"] = col.slug
    assert SLUG.match(col.slug), col.slug
    assert col.theme in ("orange", "blue", "green", "purple", "pink", "indigo"), col.theme
    assert col.items == [] and col.private is False and col.owner and col.upvotes == 0
    assert col.url.endswith("/collections/" + col.slug), col.url


@check("create_collection of the same title: 409, and exists_ok=True returns the existing one")
def _():
    assert status_of(lambda: alice.create_collection(f"Check main {tag}")) == 409
    assert alice.create_collection(f"Check main {tag}", exists_ok=True).slug == state["slug"]


@check("add_collection_item: model with a note, dataset, space, and a collection (a reference)")
def _():
    slug = state["slug"]
    child = make(alice, "Check child")
    alice.add_collection_item(slug, MODEL, "model", note="Main model, see https://example.org")
    alice.add_collection_item(slug, DATASET, "dataset")
    alice.add_collection_item(slug, SPACE, "space")
    col = alice.add_collection_item(slug, child.slug, "collection", note="A reference")
    assert [i.item_type for i in col.items] == ["model", "dataset", "space", "collection"], [i.item_type for i in col.items]
    assert [i.position for i in col.items] == [0, 1, 2, 3]
    assert col.items[0].note == "Main model, see https://example.org"
    assert col.items[3].item_id == child.slug, col.items[3].item_id   # a collection item's id is its slug
    assert all(i.item_object_id for i in col.items)
    state["child"] = child.slug


@check("add_collection_item: duplicate 409 (exists_ok returns the collection), missing repo 404, paper 501, itself 400")
def _():
    slug = state["slug"]
    assert status_of(lambda: alice.add_collection_item(slug, MODEL, "model")) == 409
    assert len(alice.add_collection_item(slug, MODEL, "model", exists_ok=True).items) == 4
    assert status_of(lambda: alice.add_collection_item(slug, "alice/does-not-exist", "model")) == 404
    assert status_of(lambda: alice.add_collection_item(slug, "2307.09288", "paper")) == 501
    assert status_of(lambda: alice.add_collection_item(slug, slug, "collection")) == 400


@check("add_collection_item: a private repository of someone else is 404 (does not leak)")
def _():
    other = make(mika, "Check mika")
    assert status_of(lambda: mika.add_collection_item(other.slug, PRIVATE, "model")) == 404


@check("update_collection_item: note and position (moving the last item to the front)")
def _():
    col = alice.get_collection(state["slug"])
    last = col.items[-1]
    alice.update_collection_item(state["slug"], last.item_object_id, note="Moved to the front", position=0)
    col = alice.get_collection(state["slug"])
    assert col.items[0].item_object_id == last.item_object_id and col.items[0].note == "Moved to the front"
    assert [i.position for i in col.items] == [0, 1, 2, 3]


@check("update_collection_metadata: title, description, theme, private; the slug follows the title, the id stays")
def _():
    old = state["slug"]
    col = alice.update_collection_metadata(old, title=f"Check renamed {tag}", description="new text", theme="pink", private=True)
    assert col.title.startswith("Check renamed") and col.theme == "pink" and col.private is True and col.description == "new text"
    assert col.slug.endswith(old[-24:]) and col.slug != old, (old, col.slug)
    state["old"], state["slug"] = old, col.slug
    created[:] = [(a, col.slug if s == old else s) for a, s in created]
    assert alice.get_collection(old).slug == col.slug      # the old slug answers 307 to the new one


@check("a private collection: 404 for anonymous and for another user, readable by its owner")
def _():
    assert status_of(lambda: anon.get_collection(state["slug"])) == 404
    assert status_of(lambda: mika.get_collection(state["slug"])) == 404
    assert alice.get_collection(state["slug"]).private is True
    assert state["slug"] not in [c.slug for c in anon.list_collections(owner="alice", limit=100)]
    assert state["slug"] in [c.slug for c in alice.list_collections(owner="alice", limit=100)]


if org:
    @check("organization collections: member writes, visitor reads (including private) but cannot write")
    def _():
        private = make(mika, "Check org", namespace=org, private=True)
        assert guest.get_collection(private.slug).title.startswith("Check org")
        assert status_of(lambda: anon.get_collection(private.slug)) == 404
        assert status_of(lambda: guest.add_collection_item(private.slug, MODEL, "model")) == 403
        mika.add_collection_item(private.slug, MODEL, "model")
        item = next(i for i in mika.get_collection(private.slug).items if i.item_id == MODEL)
        mika.delete_collection_item(private.slug, item.item_object_id)
        assert status_of(lambda: guest.create_collection(f"Visitor {tag}", namespace=org)) == 403


@check("list_collections: owner, item filter, sort, limit, q, and paging by the Link header")
def _():
    for n in range(12):                                      # more than the 10 a page has by default: the client must follow the Link header
        col = make(alice, f"Paging {n:02d}")
        if n == 0:
            alice.add_collection_item(col.slug, MODEL, "model")      # a public collection that holds the model
    mine = list(alice.list_collections(owner="alice", limit=100))
    assert len(mine) >= 14 and all(c.owner for c in mine)
    assert all(len(c.items) <= 4 for c in mine)               # a list truncates the items to 4
    hits = list(anon.list_collections(item=f"models/{MODEL}", limit=100))
    assert hits and all(any(i.item_id == MODEL for i in alice.get_collection(c.slug).items) for c in hits)
    assert len(list(anon.list_collections(sort="upvotes", limit=2))) == 2
    assert len(list(anon.list_collections(limit=3))) == 3
    every = list(anon.list_collections(owner="alice", limit=100))
    assert len({c.slug for c in every}) == len(every) >= 13, len(every)
    with urllib.request.urlopen(endpoint + f"/api/collections?q=paging%2007%20{tag}") as response:     # `q` is not a parameter of the client
        assert [c["title"] for c in json.load(response)] == [f"Paging 07 {tag}"]
    assert status_of(lambda: list(anon.list_collections(sort="newest"))) == 400


@check("paging: the client follows every Link header and visits each collection once")
def _():
    seen = [c.slug for c in anon.list_collections(owner="alice")]       # no limit: pages of the server's default size, 10
    assert len(seen) == len(set(seen)) >= 13, len(seen)


@check("the item filter does not reveal a private repository (it matches only what the caller can read)")
def _():
    leak = make(alice, "Leak check", private=False)     # a PUBLIC collection that holds a PRIVATE repository
    alice.add_collection_item(leak.slug, PRIVATE, "model")
    assert leak.slug not in [c.slug for c in anon.list_collections(item=f"models/{PRIVATE}")]
    assert leak.slug not in [c.slug for c in mika.list_collections(item=f"models/{PRIVATE}")]
    assert leak.slug in [c.slug for c in alice.list_collections(item=f"models/{PRIVATE}")]
    assert anon.get_collection(leak.slug).items == []      # and the repository is not in the collection for those who cannot read it


@check("delete_collection_item: missing item raises, missing_ok=True does not")
def _():
    col = alice.get_collection(state["slug"])
    gone = col.items[0].item_object_id
    alice.delete_collection_item(state["slug"], gone)
    assert gone not in [i.item_object_id for i in alice.get_collection(state["slug"]).items]
    assert status_of(lambda: alice.delete_collection_item(state["slug"], gone)) == 404
    alice.delete_collection_item(state["slug"], gone, missing_ok=True)


@check("delete_collection: gone afterwards; missing_ok=True on a missing one")
def _():
    alice.delete_collection(state["slug"])
    assert status_of(lambda: alice.get_collection(state["slug"])) == 404
    alice.delete_collection(state["slug"], missing_ok=True)


for api, slug in created:       # whatever is left of what this run made
    try:
        api.delete_collection(slug, missing_ok=True)
    except Exception:
        pass

print(f"huggingface_hub {huggingface_hub.__version__} against {endpoint}")
for name, ok, why in results:
    print(("PASS " if ok else "FAIL ") + name + (f"\n       {why}" if why else ""))
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)} of {len(results)} passed")
sys.exit(1 if failed else 0)
