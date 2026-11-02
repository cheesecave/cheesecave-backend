#!/usr/bin/env python3
"""Drive the collections API with the real huggingface_hub client and report what works.

usage: python scripts/dev/check_collections_with_hf_client.py ENDPOINT TOKENS.json
TOKENS.json maps user names (alice, mika, guest) to API tokens (written by seed_collections_demo.py). Needs the demo data.
"""
import json
import re
import secrets
import sys
import traceback

import huggingface_hub
from huggingface_hub import HfApi
try:
    from huggingface_hub.errors import HfHubHTTPError
except ImportError:       # before 0.26
    from huggingface_hub.utils import HfHubHTTPError

endpoint, tokens = sys.argv[1], json.load(open(sys.argv[2]))
alice, mika, guest = (HfApi(endpoint=endpoint, token=tokens[n]) for n in ("alice", "mika", "guest"))
try:
    anon = HfApi(endpoint=endpoint, token=False)    # token=False: no token even if one is saved (newer clients)
except Exception:
    anon = HfApi(endpoint=endpoint)
results = []


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


tag = secrets.token_hex(3)
title = f"Proto check {tag}"
state = {}
SLUG = re.compile(r"^alice/proto-check-[0-9a-f]{6}-[0-9a-f]{24}$")


@check("create_collection returns a Collection with a canonical slug, a theme, no items")
def _():
    col = alice.create_collection(title, description="made by the client check", private=False)
    state["slug"] = col.slug
    assert SLUG.match(col.slug), col.slug
    assert col.theme in ("orange", "blue", "green", "purple", "pink", "indigo"), col.theme
    assert col.items == [] and col.private is False and col.owner and col.upvotes == 0
    assert col.url.endswith("/collections/" + col.slug), col.url


@check("create_collection of the same title: 409, and exists_ok=True returns the existing one")
def _():
    assert status_of(lambda: alice.create_collection(title)) == 409
    assert alice.create_collection(title, exists_ok=True).slug == state["slug"]


@check("add_collection_item: model with a note, dataset, space, and a collection (a reference)")
def _():
    slug = state["slug"]
    alice.add_collection_item(slug, "alice/tagger-v3", "model", note="Main model, see https://example.org")
    alice.add_collection_item(slug, "alice/faces-2k", "dataset")
    alice.add_collection_item(slug, "alice/tagger-demo", "space")
    detection = next(c for c in alice.list_collections(owner="anime-lab") if c.title == "Detection models")
    col = alice.add_collection_item(slug, detection.slug, "collection", note="Detectors by Anime Lab")
    assert [i.item_type for i in col.items] == ["model", "dataset", "space", "collection"], [i.item_type for i in col.items]
    assert [i.position for i in col.items] == [0, 1, 2, 3]
    assert col.items[0].note == "Main model, see https://example.org"
    assert col.items[3].item_id == detection.slug, col.items[3].item_id   # a collection item's id is its slug
    assert all(i.item_object_id for i in col.items)


@check("add_collection_item: duplicate 409 (exists_ok returns the collection), missing repo 404, paper 501, itself 400")
def _():
    slug = state["slug"]
    assert status_of(lambda: alice.add_collection_item(slug, "alice/tagger-v3", "model")) == 409
    assert len(alice.add_collection_item(slug, "alice/tagger-v3", "model", exists_ok=True).items) == 4
    assert status_of(lambda: alice.add_collection_item(slug, "alice/does-not-exist", "model")) == 404
    assert status_of(lambda: alice.add_collection_item(slug, "2307.09288", "paper")) == 501
    assert status_of(lambda: alice.add_collection_item(slug, slug, "collection")) == 400


@check("add_collection_item: a private repository of someone else is 404 (does not leak)")
def _():
    assert status_of(lambda: mika.add_collection_item(mika.create_collection(f"Mika {tag}").slug, "alice/wip-private", "model")) == 404


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
    col = alice.update_collection_metadata(old, title=title + " renamed", description="new text", theme="pink", private=True)
    assert col.title.endswith("renamed") and col.theme == "pink" and col.private is True and col.description == "new text"
    assert col.slug.endswith(old[-24:]) and col.slug != old, (old, col.slug)
    state["old"], state["slug"] = old, col.slug
    assert alice.get_collection(old).slug == col.slug      # the old slug answers 307 to the new one


@check("a private collection: 404 for anonymous and for another user, readable by its owner")
def _():
    assert status_of(lambda: anon.get_collection(state["slug"])) == 404
    assert status_of(lambda: mika.get_collection(state["slug"])) == 404
    assert alice.get_collection(state["slug"]).private is True
    assert state["slug"] not in [c.slug for c in anon.list_collections(owner="alice")]
    assert state["slug"] in [c.slug for c in alice.list_collections(owner="alice")]


@check("organization collections: member writes, visitor reads (including private) but cannot write")
def _():
    private = next(c for c in guest.list_collections(owner="anime-lab") if c.private)
    assert guest.get_collection(private.slug).title == "Internal benchmarks"
    assert status_of(lambda: anon.get_collection(private.slug)) == 404
    assert status_of(lambda: guest.add_collection_item(private.slug, "mika/embeddings", "model")) == 403
    mika.add_collection_item(private.slug, "mika/embeddings", "model")
    assert any(i.item_id == "mika/embeddings" for i in mika.get_collection(private.slug).items)
    item = next(i for i in mika.get_collection(private.slug).items if i.item_id == "mika/embeddings")
    mika.delete_collection_item(private.slug, item.item_object_id)
    assert status_of(lambda: guest.create_collection(f"Visitor {tag}", namespace="anime-lab")) == 403


@check("list_collections: owner, item filter, sort, limit, q, and paging by the Link header")
def _():
    mine = list(alice.list_collections(owner="alice"))
    assert len(mine) >= 4 and all(c.owner for c in mine)
    assert all(len(c.items) <= 4 for c in mine)               # a list truncates the items to 4
    hits = list(anon.list_collections(item="models/alice/tagger-v3"))
    assert hits and all(any(i.item_id == "alice/tagger-v3" for i in alice.get_collection(c.slug).items) for c in hits)
    assert [c.slug for c in anon.list_collections(sort="upvotes", limit=2)][0] == next(c for c in anon.list_collections(sort="upvotes")).slug
    assert len(list(anon.list_collections(limit=3))) == 3
    every = list(anon.list_collections())                        # no limit: follows every Link: <...>; rel="next"
    assert len({c.slug for c in every}) == len(every) >= 6, len(every)
    import urllib.request
    with urllib.request.urlopen(endpoint + "/api/collections?q=iccv") as response:      # `q` is not a parameter of the client
        assert [c["title"] for c in json.load(response)] == ["ICCV 2026"]
    assert status_of(lambda: list(anon.list_collections(sort="newest"))) == 400


@check("the item filter does not reveal a private repository (it matches only what the caller can read)")
def _():
    leak = alice.create_collection(f"Leak check {tag}", private=False)     # a PUBLIC collection that holds a PRIVATE repository
    alice.add_collection_item(leak.slug, "alice/wip-private", "model")
    try:
        assert leak.slug not in [c.slug for c in anon.list_collections(item="models/alice/wip-private")]
        assert leak.slug not in [c.slug for c in mika.list_collections(item="models/alice/wip-private")]
        assert leak.slug in [c.slug for c in alice.list_collections(item="models/alice/wip-private")]
        # and the repository itself is not in the collection for those who cannot read it
        assert anon.get_collection(leak.slug).items == []
    finally:
        alice.delete_collection(leak.slug)


@check("delete_collection_item: missing item raises, missing_ok=True does not")
def _():
    col = alice.get_collection(state["slug"])
    gone = col.items[0].item_object_id
    alice.delete_collection_item(state["slug"], gone)
    assert gone not in [i.item_object_id for i in alice.get_collection(state["slug"]).items]
    assert status_of(lambda: alice.delete_collection_item(state["slug"], gone)) == 404
    alice.delete_collection_item(state["slug"], gone, missing_ok=True)


@check("delete_collection: gone afterwards; missing_ok=True on a missing one; also clean up the helper collection")
def _():
    alice.delete_collection(state["slug"])
    assert status_of(lambda: alice.get_collection(state["slug"])) == 404
    alice.delete_collection(state["slug"], missing_ok=True)
    mika.delete_collection(next(c.slug for c in mika.list_collections(owner="mika") if tag in c.title))


print(f"huggingface_hub {huggingface_hub.__version__} against {endpoint}")
for name, ok, why in results:
    print(("PASS " if ok else "FAIL ") + name + (f"\n       {why}" if why else ""))
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)} of {len(results)} passed")
sys.exit(1 if failed else 0)
