"""A Hugging Face shaped list endpoint for tests: pages, ``Link`` header, keyset cursor, failures.

``GET /api/{models,datasets,spaces}`` answers a bare JSON array of at most ``page_cap`` items
(the Hub's is 1000, whatever ``limit`` says) and, when more follow, a header
``Link: <absolute url with cursor>; rel="next"``. The cursor is opaque base64 of the last
``(sort value, id)``, so a page after it is stable. Like the Hub it ignores parameters it does
not know. Tests steer it through ``app.state``:

* ``items``     ``{"model": [{"id": ..., "likes": ..., ...}]}``
* ``failures``  statuses to answer the next requests with, one each (``"object"`` and
  ``"garbage"`` answer 200 with a body that is no list, ``"mixed"`` a list with entries
  that are no repositories)
* ``delay``     seconds to wait before answering
* ``calls``     ``[{"plural": "models", "query": [(name, value), ...], "authorization": ...}]``, one per request
* ``ignore_limit``  answer ``page_cap`` items whatever ``limit`` says
"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

SORT_FIELDS = {
    "trendingScore": "trendingScore",
    "createdAt": "createdAt",
    "lastModified": "lastModified",
    "likes": "likes",
    "downloads": "downloads",
}
TYPES = {"models": "model", "datasets": "dataset", "spaces": "space"}


def make_item(repo_type: str, repo_id: str, **fields) -> dict:
    owner, _, _ = repo_id.partition("/")
    item = {
        "id": repo_id,
        "author": owner,
        "private": False,
        "likes": 0,
        "downloads": 0,
        "trendingScore": 0,
        "createdAt": "2024-01-01T00:00:00.000Z",
        "tags": [],
    }
    item.update(fields)
    return item


def build_list_hf_app(items: dict[str, list[dict]] | None = None, page_cap: int = 1000) -> FastAPI:
    app = FastAPI()
    app.state.items = items or {}
    app.state.page_cap = page_cap
    app.state.failures = []
    app.state.delay = 0.0
    app.state.calls = []
    app.state.ignore_limit = False  # a source that does not honour ``limit``

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/{plural}")
    async def listing(plural: str, request: Request):
        query = list(request.query_params.multi_items())
        app.state.calls.append({"plural": plural, "query": query, "authorization": request.headers.get("authorization")})
        if app.state.delay:
            await asyncio.sleep(app.state.delay)
        if app.state.failures:
            failure = app.state.failures.pop(0)
            if failure == "object":
                return JSONResponse({"error": "not a list"})
            if failure == "mixed":
                return JSONResponse([make_item("model", "acme/ok"), "stray", 3])
            if failure == "garbage":
                return Response("<html>", media_type="application/json")
            return Response(status_code=failure)

        params = {}
        for name, value in query:
            params.setdefault(name, []).append(value)
        rows = list(app.state.items.get(TYPES.get(plural, ""), []))
        if "author" in params:
            rows = [r for r in rows if r["id"].startswith(params["author"][0] + "/")]
        if "search" in params:
            rows = [r for r in rows if params["search"][0].lower() in r["id"].lower()]
        for tag in params.get("filter", []):
            rows = [r for r in rows if tag in r.get("tags", [])]
        if "pipeline_tag" in params:
            rows = [r for r in rows if r.get("pipeline_tag") == params["pipeline_tag"][0]]
        if "library" in params:
            rows = [r for r in rows if r.get("library_name") == params["library"][0]]

        field = SORT_FIELDS.get(params.get("sort", ["trendingScore"])[0], "trendingScore")
        ascending = params.get("direction", ["-1"])[0] == "1"
        rows = sorted(sorted(rows, key=lambda r: r["id"]), key=lambda r: r.get(field, 0), reverse=not ascending)

        if "cursor" in params:
            try:
                last = json.loads(base64.urlsafe_b64decode(params["cursor"][0] + "=="))["id"]
            except Exception:
                return Response(status_code=400)
            ids = [r["id"] for r in rows]
            rows = rows[ids.index(last) + 1 :] if last in ids else []

        asked = int(params["limit"][0]) if "limit" in params and not app.state.ignore_limit else app.state.page_cap
        size = min(asked, app.state.page_cap)
        page, rest = rows[:size], rows[size:]
        headers = {}
        if rest:
            cursor = base64.urlsafe_b64encode(json.dumps({"id": page[-1]["id"]}).encode()).decode().rstrip("=")
            kept = [(n, v) for n, v in query if n != "cursor"] + [("cursor", cursor)]
            headers["Link"] = f'<{request.base_url}api/{plural}?{urlencode(kept)}>; rel="next"'
        return JSONResponse(page, headers=headers)

    return app
