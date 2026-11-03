"""What the collections API sends and accepts, against Hugging Face's own OpenAPI schemas (#134).

``test/kohakuhub/support/hf_collections_openapi.json`` holds, unchanged except that descriptions are dropped, the request
and response schemas of the collections operations that ``huggingface_hub`` uses, taken from
https://huggingface.co/.well-known/openapi.json. Every document the API answers here must satisfy them, with the item
variants (model, dataset, space, collection), the list entry and the 409 body included; so must the bodies a client sends.
Real database, as in ``test_collections.py`` (whose fixtures this module uses).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from test.kohakuhub.api.test_collections import add, api, auth, create, made, world  # noqa: F401  (fixtures and helpers)

SPEC = json.loads((Path(__file__).parents[1] / "support" / "hf_collections_openapi.json").read_text())["paths"]
ONE = "/api/collections/{namespace}/{slug}-{id}"


def response_schema(path, method, code="200"):
    schema = copy.deepcopy(SPEC[path][method]["responses"][code]["content"]["application/json"]["schema"])
    if method == "patch" and schema.get("required") == ["success", "data"]:
        # The spec cannot be satisfied as written: it requires `success` and forbids every property but `data`. The answer
        # carries both (the client reads `data`), so the clause that forbids the other property is dropped for this schema.
        schema.pop("additionalProperties", None)
        schema["properties"]["success"] = {"type": "boolean"}
    return schema


def request_schema(path, method):
    return SPEC[path][method]["requestBody"]["content"]["application/json"]["schema"]


def problems(schema, document):
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message[:200]}" for e in Draft202012Validator(schema).iter_errors(document)]


def test_the_schemas_in_the_fixture_reject_what_they_should():
    # a check that does not fail on a wrong document checks nothing
    assert problems(response_schema(ONE, "get"), {"slug": 1})
    assert problems(request_schema("/api/collections", "post"), {"title": 5})


def test_what_a_client_sends_is_what_the_spec_allows():
    assert not problems(request_schema("/api/collections", "post"), {
        "title": "t", "namespace": "alice", "private": False, "description": "x", "item": {"type": "model", "id": "alice/tagger"},
    })
    assert not problems(request_schema(ONE + "/items", "post"), {"item": {"type": "dataset", "id": "alice/faces"}, "note": "n"})
    assert not problems(request_schema(ONE, "patch"), {"title": "t", "theme": "pink", "private": False, "position": 0, "description": "d"})


def test_the_answers_for_a_collection_with_every_kind_of_item_satisfy_the_spec(api, world):
    created = create(api, world, title="Spec", namespace="alice", item={"type": "model", "id": "alice/tagger"})
    assert created.status_code == 200
    assert not problems(response_schema("/api/collections", "post"), created.json())
    slug = created.json()["slug"]
    conflict = create(api, world, title="Spec", namespace="alice")
    assert conflict.status_code == 409
    assert not problems(response_schema("/api/collections", "post", "409"), conflict.json())
    child = made(api, world, title="Spec child")
    for kind, ident, label in (
        ("dataset", "alice/faces", "dataset"), ("collection", child["slug"], "collection"), ("space", "alice/demo", "space"),
    ):
        answer = add(api, world, slug, kind, ident, note="Plain text https://example.org <b>")
        assert answer.status_code == 200, label
        assert not problems(response_schema(ONE + "/items", "post"), answer.json()), label
    got = api.get(f"/api/collections/{slug}", headers=auth(world, "alice"))
    assert not problems(response_schema(ONE, "get"), got.json())
    patched = api.patch(f"/api/collections/{slug}", json={"theme": "indigo", "description": "d"}, headers=auth(world, "alice"))
    assert not problems(response_schema(ONE, "patch"), patched.json())


def test_the_answers_for_a_list_and_for_a_light_list_satisfy_the_spec(api, world):
    made(api, world, title="First", items=[("model", "alice/tagger"), ("dataset", "alice/faces")])
    made(api, world, title="Second", items=[("space", "alice/demo")])
    for query in ("limit=5", "owner=alice&limit=100", "owner=alice&expand=false&limit=200", "sort=upvotes&limit=2", "item=models/alice/tagger"):
        answer = api.get("/api/collections?" + query, headers=auth(world, "alice"))
        assert answer.status_code == 200 and answer.json(), query
        assert not problems(response_schema("/api/collections", "get"), answer.json()), query
