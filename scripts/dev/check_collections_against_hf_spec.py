#!/usr/bin/env python3
"""Validate the collections answers against the JSON Schemas in Hugging Face's own OpenAPI spec.

usage: python scripts/dev/check_collections_against_hf_spec.py ENDPOINT TOKENS.json SPEC.json
SPEC.json is https://huggingface.co/.well-known/openapi.json (fetched by hand: it is 1.2 MB). Needs jsonschema.
"""
import copy
import json
import secrets
import sys
import urllib.error
import urllib.request

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

endpoint, tokens, spec = sys.argv[1], json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))
components = spec["components"]
P = "/api/collections/{namespace}/{slug}-{id}"
problems, checked = [], 0


def call(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {tokens[token]}"
    request = urllib.request.Request(endpoint + path, method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, json.loads(raw) if raw else None


def validate(label, schema_of_spec, payload):
    global checked
    schema = copy.deepcopy(schema_of_spec)
    schema["components"] = components            # so that "#/components/schemas/..." resolves inside this document
    errors = list(Draft202012Validator(schema).iter_errors(payload))
    checked += 1
    seen = set()
    for error in errors:
        # for a failed anyOf/oneOf the useful message is in the branch that came closest
        leaf = best_match(error.context) if error.context else error
        where = "/".join(str(p) for p in list(error.path) + (list(leaf.path) if leaf is not error else []))
        text = f"{label}: at {where or '<root>'}: {leaf.message[:140]}"
        if text not in seen:
            seen.add(text)
            problems.append(text)


def response_schema(path, method, code="200"):
    schema = copy.deepcopy(spec["paths"][path][method]["responses"][code]["content"]["application/json"]["schema"])
    if method == "patch" and schema.get("required") == ["success", "data"]:
        # The spec cannot be satisfied here: it requires `success` and forbids every property but `data`. The answer
        # carries both (the client reads `data`), so the "no other properties" clause is dropped for this one schema.
        schema.pop("additionalProperties", None)
        schema["properties"]["success"] = {"type": "boolean"}
    return schema


def request_schema(path, method):
    return spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]


tag = secrets.token_hex(3)
# what we send must be something the spec allows, too
for label, path, method, body in (
    ("create body", "/api/collections", "post", {"title": f"Spec check {tag}", "namespace": "alice", "private": False, "description": "x", "item": {"type": "model", "id": "alice/tagger-v3"}}),
    ("add item body", P + "/items", "post", {"item": {"type": "dataset", "id": "alice/faces-2k"}, "note": "n"}),
    ("update body", P, "patch", {"title": "t", "theme": "pink", "private": False, "position": 0, "description": "d"}),
):
    validate(label, request_schema(path, method), body)

status, created = call("POST", "/api/collections", "alice", {"title": f"Spec check {tag}", "namespace": "alice", "item": {"type": "model", "id": "alice/tagger-v3"}})
assert status == 200, (status, created)
slug = created["slug"]
validate("POST /api/collections 200", response_schema("/api/collections", "post"), created)
status, conflict = call("POST", "/api/collections", "alice", {"title": f"Spec check {tag}", "namespace": "alice"})
validate("POST /api/collections 409", response_schema("/api/collections", "post", "409"), conflict)
status, got = call("GET", f"/api/collections/{slug}", "alice")
validate("GET collection 200", response_schema(P, "get"), got)
status, with_items = call("POST", f"/api/collections/{slug}/items", "alice", {"item": {"type": "dataset", "id": "alice/faces-2k"}, "note": "Plain text https://example.org <b>"})
validate("POST items 200", response_schema(P + "/items", "post"), with_items)
status, nested = call("POST", f"/api/collections/{slug}/items", "alice", {"item": {"type": "collection", "id": next(c["slug"] for c in call("GET", "/api/collections?owner=anime-lab", "alice")[1])}})
validate("POST items 200 (with a collection item)", response_schema(P + "/items", "post"), nested)
status, spaced = call("POST", f"/api/collections/{slug}/items", "alice", {"item": {"type": "space", "id": "alice/tagger-demo"}})
validate("POST items 200 (with a space)", response_schema(P + "/items", "post"), spaced)
status, patched = call("PATCH", f"/api/collections/{slug}", "alice", {"theme": "indigo", "description": "d"})
validate("PATCH collection 200", response_schema(P, "patch"), patched)
status, listing = call("GET", "/api/collections?limit=5")
validate("GET /api/collections 200", response_schema("/api/collections", "get"), listing)
status, light = call("GET", "/api/collections?owner=alice&expand=false&limit=200")
validate("GET /api/collections expand=false 200", response_schema("/api/collections", "get"), light)
call("DELETE", f"/api/collections/{slug}", "alice")

print(f"{checked} documents checked against {len(spec['paths'])} paths of the spec")
for problem in problems:
    print("  -", problem)
print("OK: nothing the spec rejects" if not problems else f"{len(problems)} problem(s)")
sys.exit(1 if problems else 0)
