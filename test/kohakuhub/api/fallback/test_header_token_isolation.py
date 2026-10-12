"""A caller's own external token never changes what other callers are served.

The fallback binding cache is keyed by user and by a fingerprint of the caller's
effective tokens, stores only which source holds a repository (never a token and
never a response body), and a header token only replaces the token of the source
whose URL it names. Driven through the real routes with a real local upstream
that, like a private mirror, answers only to one token.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SECRET = "s3cret"
REPO = "ghost/private-thing"
INFO = "/api/models/" + REPO


class _PrivateMirror(BaseHTTPRequestHandler):
    seen_tokens: list = []

    def log_message(self, *args):
        pass

    def _answer(self):
        token = self.headers.get("Authorization")
        type(self).seen_tokens.append(token)
        allowed = token == f"Bearer {SECRET}"
        body = json.dumps({"id": REPO, "modelId": REPO, "siblings": []}).encode() if allowed else b""
        self.send_response(200 if allowed else 404)
        if not allowed:
            self.send_header("X-Error-Code", "RepoNotFound")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(body)

    do_GET = do_HEAD = _answer


@pytest.fixture
def mirror(backend_test_state):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PrivateMirror)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    cfg = backend_test_state.modules.config_module.cfg
    cache_module = backend_test_state.modules.fallback_cache_module
    old = (list(cfg.fallback.sources), cfg.fallback.enabled)
    cfg.fallback.enabled = True
    cfg.fallback.sources = [
        {"url": url, "name": "private-mirror", "source_type": "kohakuhub", "priority": 1}
    ]
    cache_module.get_cache().clear()
    _PrivateMirror.seen_tokens = []
    yield url
    cfg.fallback.sources, cfg.fallback.enabled = old
    cache_module.get_cache().clear()
    server.shutdown()


def _with_token(url: str, token: str) -> dict:
    return {"Authorization": f"Bearer |{url},{token}"}


async def test_only_the_caller_holding_the_token_is_served(client, mirror):
    served = await client.get(INFO, headers=_with_token(mirror, SECRET))
    assert served.status_code == 200, served.text

    for headers in (None, _with_token(mirror, "nope"), _with_token("http://elsewhere.example", SECRET)):
        refused = await client.get(INFO, headers=headers)
        assert refused.status_code == 404, (headers, refused.text)

    again = await client.get(INFO, headers=_with_token(mirror, SECRET))
    assert again.status_code == 200


async def test_a_signed_in_user_without_the_token_is_not_served_after_someone_else_was(
    client, owner_client, mirror
):
    # an anonymous caller with the token binds the repository first
    anonymous = await client.get(INFO, headers=_with_token(mirror, SECRET))
    assert anonymous.status_code == 200

    # the signed-in owner, without a token of their own, still gets the upstream's answer
    refused = await owner_client.get(INFO)
    assert refused.status_code == 404


async def test_the_cache_holds_no_token_and_no_body(client, mirror, backend_test_state):
    served = await client.get(INFO, headers=_with_token(mirror, SECRET))
    assert served.status_code == 200

    cache = backend_test_state.modules.fallback_cache_module.get_cache()
    stored = json.dumps(list(cache.cache.values()))
    assert stored != "[]"
    assert SECRET not in stored
    assert "siblings" not in stored


async def test_a_header_token_replaces_only_the_token_of_the_source_it_names(client, mirror):
    await client.get(INFO, headers=_with_token(mirror, SECRET))

    assert set(_PrivateMirror.seen_tokens) == {f"Bearer {SECRET}"}
